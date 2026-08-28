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
    manifestId: t.manifest_id ?? undefined,
    manifestStatus: t.manifest_status ?? undefined,
    fieldCount: t.field_count ?? 0,
    conditionCount: t.condition_count ?? 0,
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
  loadProjectDetail: (id: string) => Promise<void>;
  createProject: (input: CreateProjectInput) => Promise<string>;
  getProject: (id: string) => Project | undefined;
  addTemplate: (projectId: string, file: File) => Promise<void>;
  addSource: (projectId: string, file: File) => Promise<void>;
  setGenerationMethod: (projectId: string, method: string, model?: string, temperature?: number) => Promise<void>;
  refreshGenerated: (projectId: string) => Promise<void>;
}

export const useStore = create<Store>((set, get) => ({
  currentUser: "",

  loadCurrentUser: async () => {
    const me = await api.me();
    set({ currentUser: me.full_name });
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

  addTemplate: async (projectId, file) => {
    await api.uploadTemplate(projectId, file, file.name);
    await get().loadProjectDetail(projectId);
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
