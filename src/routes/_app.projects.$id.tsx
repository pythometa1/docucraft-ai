import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { useStore } from "@/lib/store";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { CompileProgressList, useCompileProgress } from "@/components/compile-progress";
import { DocumentMapping } from "@/components/document-mapping";
import {
  ChevronDown,
  ChevronRight,
  Upload,
  UploadCloud,
  FolderTree,
  Network,
  FileText,
  Plus,
  MoreVertical,
  Download,
  Pencil,
  Eye,
  Trash2,
  Share2,
  Archive,
  Layout,
  LayoutGrid,
  Check,
  Wand2,
  Wand2 as WandIcon,
  Loader2,
} from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

export const Route = createFileRoute("/_app/projects/$id")({
  head: ({ params }) => ({
    meta: [
      { title: `Project ${params.id} — DocuMind AI` },
      { name: "description", content: "Configure templates, sources, mappings, and generate documents." },
    ],
  }),
  component: ProjectDetail,
});

const STAGES = [
  { key: "template", n: 1, title: "Template", short: "Blueprint", icon: UploadCloud, hint: "Upload the document to fill" },
  { key: "source", n: 2, title: "Sources", short: "Inputs", icon: FolderTree, hint: "Upload the spreadsheet of rows" },
  { key: "mapping2", n: 3, title: "Document Mapping", short: "Fill", icon: Network, hint: "Compile, map columns, generate" },
  { key: "drafts", n: 4, title: "Documents", short: "Output", icon: FileText, hint: "Everything this project has produced" },
] as const;

type StageKey = typeof STAGES[number]["key"];

function ProjectDetail() {
  const { id } = Route.useParams();
  const project = useStore((s) => s.projects.find((p) => p.id === id));
  const loadProjectDetail = useStore((s) => s.loadProjectDetail);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Every hook has to run on every render, and `project` is undefined on the
  // first render of a cold load (direct URL / refresh). So this starts null and
  // falls back to the first incomplete stage below, rather than seeding itself
  // from data that isn't there yet.
  const [active, setActive] = useState<StageKey | null>(null);

  useEffect(() => {
    // Cleared first: navigating from a project that failed to load to one that
    // loads fine otherwise leaves the previous project's error on screen for
    // good, because nothing else ever resets it.
    setLoadError(null);
    loadProjectDetail(id).catch((e: any) => setLoadError(e?.message ?? String(e)));
  }, [id]);

  const done: Record<StageKey, boolean> = {
    // Uploading is not the same as being ready. A template nobody has compiled
    // tells the rest of the pipeline nothing about what data the letter needs,
    // so the stage stays open until at least one has been read.
    template: (project?.templates ?? []).some((t: any) => !!t.manifestId),
    source: (project?.sources.length ?? 0) > 0,
    // These were the same expression, so the counter went straight from 2/4 to
    // 4/4 and could never read 3/4. Generation having run is what says the
    // mapping stage was completed; a document that survived the §19 canary gate
    // is what says the project actually has an output. A batch where every row
    // came back "blocked" is the case that has to tell those apart.
    mapping2: (project?.generated.length ?? 0) > 0,
    drafts: (project?.generated ?? []).some((g) => g.status !== "blocked"),
  };
  const firstIncomplete = (STAGES.find((s) => !done[s.key])?.key ?? "drafts") as StageKey;

  if (loadError) {
    return (
      <div className="p-8 max-w-lg mx-auto text-center space-y-3">
        <p className="text-muted-foreground">This project couldn't be loaded: {loadError}</p>
        <Link to="/dashboard" className="text-brand hover:underline">Back to dashboard</Link>
      </div>
    );
  }
  if (!project) {
    return <div className="p-8 text-muted-foreground">Loading project…</div>;
  }

  const activeKey = active ?? firstIncomplete;
  const activeIdx = STAGES.findIndex((s) => s.key === activeKey);
  const activeStage = STAGES[activeIdx];
  const completedCount = Object.values(done).filter(Boolean).length;
  const progressPct = (completedCount / STAGES.length) * 100;

  return (
    <div className="p-6 md:p-8 max-w-7xl mx-auto space-y-6">
      {/* Breadcrumb */}
      <div className="text-sm text-muted-foreground flex items-center gap-2">
        <Link to="/dashboard" className="hover:text-foreground">Projects</Link>
        <ChevronRight className="h-3.5 w-3.5" />
        <span className="text-foreground">{project.name}</span>
      </div>

      {/* Title + meta strip */}
      <div className="rounded-2xl border border-border bg-surface overflow-hidden">
        <div className="p-6 flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-xs font-mono text-muted-foreground">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-brand" />
              {project.projectId} · {project.region} · {project.function}
            </div>
            <h1 className="text-2xl md:text-3xl font-bold tracking-tight mt-1.5">{project.name}</h1>
            {project.description && (
              <p className="text-sm text-muted-foreground mt-1 max-w-2xl">{project.description}</p>
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <div className="text-right pr-3 border-r border-border">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Progress</div>
              <div className="text-sm font-semibold">{completedCount}/{STAGES.length} stages</div>
            </div>
            <ShareButton />
            <ProjectActions project={project} />
          </div>
        </div>
        {/* Progress bar */}
        <div className="h-1 bg-muted">
          <div className="h-full bg-gradient-brand transition-all" style={{ width: `${progressPct}%` }} />
        </div>
      </div>

      {/* Pipeline rail */}
      <PipelineRail stages={STAGES} done={done} active={activeKey} onSelect={setActive} />

      {/* Active stage panel */}
      <div className="rounded-2xl border border-border bg-surface">
        <div className="flex items-center gap-4 p-6 border-b border-border">
          <div className={cn(
            "h-11 w-11 rounded-xl flex items-center justify-center border",
            done[activeKey] ? "bg-success/10 text-success border-success/30" : "bg-brand/10 text-brand border-brand/30",
          )}>
            <activeStage.icon className="h-5 w-5" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-[11px] uppercase tracking-wider text-muted-foreground font-mono">Stage {activeStage.n} of {STAGES.length}</div>
            <div className="text-lg font-semibold">{activeStage.title}</div>
            <div className="text-sm text-muted-foreground">{activeStage.hint}</div>
          </div>
          <div className="flex items-center gap-2">
            <button
              disabled={activeIdx === 0}
              onClick={() => setActive(STAGES[activeIdx - 1].key)}
              className="h-9 px-3 rounded-lg border border-border hover:bg-accent text-sm disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Previous
            </button>
            <button
              disabled={activeIdx === STAGES.length - 1}
              onClick={() => setActive(STAGES[activeIdx + 1].key)}
              className="h-9 px-3 rounded-lg bg-gradient-brand text-white text-sm inline-flex items-center gap-1 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Next stage <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
        <div className="p-6">
          {activeKey === "template" && <Step1Template project={project} />}
          {activeKey === "source" && <Step2Source project={project} />}
          {activeKey === "drafts" && <StageDocuments project={project} />}
          {activeKey === "mapping2" && <DocumentMapping project={project} />}
        </div>
      </div>
    </div>
  );
}

/* ------------------ Header actions ------------------ */

/* There is no sharing endpoint -- no invites, no share links, no per-project
   ACL. Rather than leave a button that promises one, this copies the project
   URL, which is all "share" can honestly mean today: anyone who can already
   sign in to this workspace can open it. */
function ShareButton() {
  const [copying, setCopying] = useState(false);
  const copy = async () => {
    setCopying(true);
    try {
      await navigator.clipboard.writeText(window.location.href);
      toast.success("Project link copied", { description: "Anyone who can sign in to this workspace can open it." });
    } catch (e: any) {
      toast.error("Couldn't copy the link", { description: e?.message ?? String(e) });
    } finally {
      setCopying(false);
    }
  };
  return (
    <button
      onClick={copy}
      disabled={copying}
      className="h-9 px-3 rounded-lg border border-border bg-surface hover:bg-accent text-sm inline-flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
      title="Copy a link to this project"
    >
      <Share2 className="h-4 w-4" /> Share
    </button>
  );
}

function ProjectActions({ project }: { project: any }) {
  const navigate = useNavigate();
  const loadProjectDetail = useStore((s) => s.loadProjectDetail);
  const [renameOpen, setRenameOpen] = useState(false);
  const [name, setName] = useState(project.name);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState<null | "rename" | "archive" | "delete">(null);

  const rename = async () => {
    const next = name.trim();
    if (!next || next === project.name) {
      setRenameOpen(false);
      return;
    }
    setBusy("rename");
    try {
      await api.patchProject(project.id, { name: next });
      await loadProjectDetail(project.id);
      toast.success("Project renamed", { description: next });
      setRenameOpen(false);
    } catch (e: any) {
      toast.error("Rename failed", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  };

  const archive = async () => {
    setBusy("archive");
    try {
      await api.archiveProject(project.id);
      await loadProjectDetail(project.id);
      toast.success("Project archived", { description: project.name });
    } catch (e: any) {
      toast.error("Archive failed", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    setBusy("delete");
    try {
      await api.deleteProject(project.id);
      toast.success("Project deleted", { description: project.name });
      setConfirmDelete(false);
      // No refresh here: the record this page renders is gone, so leave first
      // and let the dashboard reload the list.
      navigate({ to: "/dashboard" });
    } catch (e: any) {
      toast.error("Delete failed", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            disabled={busy != null}
            className="h-9 w-9 rounded-lg border border-border bg-surface hover:bg-accent flex items-center justify-center disabled:opacity-50 disabled:cursor-not-allowed"
            title="Project actions"
          >
            <MoreVertical className="h-4 w-4" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-44">
          <DropdownMenuItem onSelect={() => { setName(project.name); setRenameOpen(true); }}>
            <Pencil className="h-4 w-4 mr-2" /> Rename
          </DropdownMenuItem>
          <DropdownMenuItem disabled={busy === "archive"} onSelect={() => { void archive(); }}>
            <Archive className="h-4 w-4 mr-2" /> Archive
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            className="text-destructive focus:text-destructive"
            onSelect={() => setConfirmDelete(true)}
          >
            <Trash2 className="h-4 w-4 mr-2" /> Delete
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={renameOpen} onOpenChange={(v) => { if (busy !== "rename") setRenameOpen(v); }}>
        <DialogContent className="bg-surface border-border">
          <DialogHeader><DialogTitle>Rename project</DialogTitle></DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="project-name">Project name</Label>
            <Input
              id="project-name"
              value={name}
              autoFocus
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void rename(); }}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" disabled={busy === "rename"} onClick={() => setRenameOpen(false)}>Cancel</Button>
            <Button
              disabled={busy === "rename" || !name.trim()}
              onClick={() => { void rename(); }}
              className="bg-gradient-brand text-white hover:opacity-90"
            >
              {busy === "rename" ? "Saving…" : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        busy={busy === "delete"}
        title={`Delete "${project.name}"?`}
        description="This permanently removes the project along with its templates, sources, manifests and generated documents. This cannot be undone."
        confirmLabel="Delete project"
        onConfirm={remove}
      />
    </>
  );
}

function PipelineRail({
  stages,
  done,
  active,
  onSelect,
}: {
  stages: typeof STAGES;
  done: Record<StageKey, boolean>;
  active: StageKey;
  onSelect: (k: StageKey) => void;
}) {
  return (
    <div className="rounded-2xl border border-border bg-surface p-4">
      {/* Column count comes from the stage list, not a literal. It was hardcoded
          to five, so adding a sixth stage rendered it onto a second row with no
          heading -- present in the DOM, invisible on the page. */}
      <div
        className="grid gap-3 relative"
        style={{ gridTemplateColumns: `repeat(${stages.length}, minmax(0, 1fr))` }}
      >
        {stages.map((s, i) => {
          const isActive = active === s.key;
          const isDone = done[s.key];
          return (
            <div key={s.key} className="relative">
              {/* Connector line */}
              {i < stages.length - 1 && (
                <div className={cn(
                  "hidden md:block absolute top-5 left-[calc(50%+22px)] right-[-12px] h-px",
                  done[stages[i + 1].key] || (isDone && !done[stages[i + 1].key]) ? "bg-brand/60" : "bg-border",
                )} />
              )}
              <button
                onClick={() => onSelect(s.key)}
                className="w-full flex flex-col items-center text-center gap-2 group"
              >
                <div className={cn(
                  "h-10 w-10 rounded-full flex items-center justify-center border-2 font-mono text-sm font-semibold transition-all relative z-10",
                  isActive
                    ? "bg-gradient-brand text-white border-transparent shadow-lg shadow-brand/30 scale-110"
                    : isDone
                    ? "bg-success/10 text-success border-success/40"
                    : "bg-background text-muted-foreground border-border group-hover:border-border-strong group-hover:text-foreground",
                )}>
                  {isDone && !isActive ? <Check className="h-4 w-4" /> : s.n}
                </div>
                <div className="min-w-0">
                  <div className={cn(
                    "text-sm font-semibold truncate",
                    isActive ? "text-foreground" : isDone ? "text-foreground" : "text-muted-foreground",
                  )}>{s.title}</div>
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono truncate">
                    {isDone ? "Complete" : isActive ? "Current" : s.short}
                  </div>
                </div>
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* Compatibility shim: renders children only (header lives in the stage panel now). */
function StepCard({ children }: { n?: number; title?: string; count?: number; description?: string; icon?: any; iconColor?: string; status?: string; defaultOpen?: boolean; children: React.ReactNode }) {
  return <div className="space-y-4">{children}</div>;
}

/* ------------------ Step 1 ------------------ */
function Step1Template({ project }: { project: any }) {
  const [uploadOpen, setUploadOpen] = useState(false);
  const add = useStore((s) => s.addTemplate);
  const loadProjectDetail = useStore((s) => s.loadProjectDetail);
  const count = project.templates.length;
  return (
    <StepCard
      n={1}
      title="Import template file"
      count={count}
      description="Template file defines the structure for generated documents"
      icon={UploadCloud}
      iconColor="bg-info/15 text-info"
      status={count > 0 ? "Completed" : "Pending"}
      defaultOpen
    >
      {count === 0 ? (
        <EmptyState
          illustration={<TemplateBox />}
          title="You don't have any template file yet!"
          subtitle="Import a template file to get started and define your document structure."
          action={
            <Button onClick={() => setUploadOpen(true)} className="bg-gradient-brand text-white hover:opacity-90">
              <Upload className="h-4 w-4 mr-1.5" /> Import template file
            </Button>
          }
        />
      ) : (
        <div className="space-y-2">
          {project.templates.map((t: any) => (
            <div key={t.id} className="flex items-center gap-3 rounded-lg border border-border bg-background/40 p-3">
              <FileText className="h-5 w-5 text-info" />
              <div className="flex-1">
                <div className="font-medium text-sm">{t.name}</div>
                <div className="text-xs text-muted-foreground">
                  {t.size} · uploaded {t.uploadedAt} by {t.uploadedBy}
                </div>
                {/* Compiling is a fact about the template, so it is stated on the
                    template. Until it has happened nothing downstream knows what
                    data the letter needs, which is why this stage is not complete
                    without it. */}
                <div className="mt-1 text-xs">
                  {t.manifestId ? (
                    <span className="text-emerald-500">
                      Compiled · {t.fieldCount} fields, {t.conditionCount} conditions
                      {t.manifestStatus === "approved" ? " · approved" : ""}
                    </span>
                  ) : (
                    <span className="text-amber-500">Not compiled yet — open it in Studio to read it</span>
                  )}
                </div>
              </div>
              {t.manifestId && <TemplateDataButton manifestId={t.manifestId} />}
              {!t.manifestId && <CompileTemplateButton templateId={t.id} projectId={project.id} />}
              <Link
                to="/projects/$id/studio/$templateId"
                params={{ id: project.id, templateId: t.id }}
                className="h-8 px-3 rounded-lg border border-border text-xs inline-flex items-center gap-1.5 hover:bg-accent"
                title="Inspect the compiled manifest, its warnings and its conditions"
              >
                <WandIcon className="h-3.5 w-3.5" /> Studio
              </Link>
              <RowDeleteButton
                label="Delete template"
                title={`Delete "${t.name}"?`}
                description="The template is removed from this project. Manifests already compiled from it, and any documents already generated, are kept."
                confirmLabel="Delete template"
                successMessage="Template deleted"
                errorMessage="Couldn't delete the template"
                onDelete={async () => {
                  await api.deleteTemplate(t.id);
                  await loadProjectDetail(project.id);
                }}
              />
            </div>
          ))}
          <Button variant="outline" onClick={() => setUploadOpen(true)}>
            <Plus className="h-4 w-4 mr-1.5" /> Add another template
          </Button>
        </div>
      )}
      <UploadDialog
        open={uploadOpen}
        onOpenChange={setUploadOpen}
        title="Upload template"
        accept=".docx,.dotx"
        onUpload={(file) => {
          add(project.id, file)
            .then(() => toast.success("Template uploaded", { description: file.name }))
            .catch((e: any) => toast.error("Upload failed", { description: e?.message ?? String(e) }));
        }}
      />
    </StepCard>
  );
}

/* ------------------ Step 2 ------------------ */
function Step2Source({ project }: { project: any }) {
  const [uploadOpen, setUploadOpen] = useState(false);
  const add = useStore((s) => s.addSource);
  const loadProjectDetail = useStore((s) => s.loadProjectDetail);
  const count = project.sources.length;
  return (
    <StepCard
      n={2}
      title="Import source files"
      count={count}
      description="Source files provide the content used to fill template sections"
      icon={UploadCloud}
      iconColor="bg-purple/15 text-purple"
      status={count > 0 ? "Completed" : "Pending"}
    >
      {count === 0 ? (
        <EmptyState
          illustration={<SourceBox />}
          title="You don't have any source files yet!"
          subtitle="Import one or more source files to provide the content used to generate the document."
          action={
            <Button onClick={() => setUploadOpen(true)} className="bg-gradient-brand text-white hover:opacity-90">
              <Upload className="h-4 w-4 mr-1.5" /> Import source files
            </Button>
          }
        />
      ) : (
        <div className="grid md:grid-cols-2 gap-3">
          {project.sources.map((s: any) => (
            <div key={s.id} className="rounded-lg border border-border bg-background/40 p-4">
              <div className="flex items-center gap-2 mb-2">
                <FileText className="h-4 w-4 text-purple" />
                <div className="font-medium text-sm truncate flex-1">{s.name}</div>
                <RowDeleteButton
                  label="Delete source"
                  title={`Delete "${s.name}"?`}
                  description="The source is removed from this project. Column mappings already saved against it are kept, and so is anything already generated. The uploaded file and the embeddings built from it are destroyed later by the retention sweep, on the schedule your organisation set."
                  confirmLabel="Delete source"
                  successMessage="Source deleted"
                  errorMessage="Couldn't delete the source"
                  onDelete={async () => {
                    await api.deleteSource(s.id);
                    await loadProjectDetail(project.id);
                  }}
                />
              </div>
              <div className="text-xs text-muted-foreground">{s.type.toUpperCase()} · {s.rows ?? "—"} rows · {s.size}</div>
            </div>
          ))}
          <button
            onClick={() => setUploadOpen(true)}
            className="rounded-lg border border-dashed border-border p-4 text-sm text-muted-foreground hover:text-foreground hover:border-border-strong"
          >
            <Plus className="h-4 w-4 inline mr-1.5" /> Add another source
          </button>
        </div>
      )}
      <UploadDialog
        open={uploadOpen}
        onOpenChange={setUploadOpen}
        title="Upload source file"
        accept=".csv,.xlsx,.pdf,.docx,.txt"
        onUpload={(file) => {
          add(project.id, file)
            .then(() => toast.success("Source uploaded", { description: file.name }))
            .catch((e: any) => toast.error("Upload failed", { description: e?.message ?? String(e) }));
        }}
      />
    </StepCard>
  );
}

/* ------------------ Step 4: generated documents ------------------ */

/* The download endpoint is /document-versions/{id}/download, but a row here
   carries the *document* id -- the store drops current_version_id when it maps
   the API response -- so resolve the current version before asking for a file. */
async function downloadDocument(documentId: string, filename: string) {
  const doc = await api.getDocument(documentId);
  const versionId = doc?.current_version_id;
  if (!versionId) throw new Error("This document has no saved version to download yet.");
  const url = await api.authedDownloadUrl(versionId);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function DownloadDocButton({ documentId, filename }: { documentId: string; filename: string }) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      onClick={async () => {
        setBusy(true);
        try {
          await downloadDocument(documentId, filename);
        } catch (e: any) {
          toast.error("Download failed", { description: e?.message ?? String(e) });
        } finally {
          setBusy(false);
        }
      }}
      disabled={busy}
      className="p-1.5 rounded hover:bg-accent text-muted-foreground disabled:opacity-40 disabled:cursor-not-allowed"
      title="Download"
      aria-label="Download"
    >
      <Download className="h-4 w-4" />
    </button>
  );
}


function StageDocuments({ project }: { project: any }) {
  const loadProjectDetail = useStore((s) => s.loadProjectDetail);
  const count = project.generated.length;
  return (
    <StepCard
      n={4}
      title="Documents"
      count={count}
      description="The letters generated from this template and your source data"
      icon={Network}
      iconColor="bg-brand/15 text-brand"
      status={count > 0 ? "Completed" : "Pending"}
    >
      {count === 0 ? (
        <EmptyState
          illustration={<NetworkNodes />}
          title="No generated documents yet."
          subtitle="Complete a mapping to generate documents."
        />
      ) : (
        <div className="space-y-2">
          {project.generated.map((g: any) => (
            <div key={g.id} className="flex items-center gap-3 rounded-lg border border-border bg-background/40 p-3">
              <FileText className="h-5 w-5 text-brand" />
              <div className="flex-1 min-w-0">
                <div className="font-medium text-sm truncate">{g.filename}</div>
                <div className="text-xs text-muted-foreground">
                  {g.generatedAt} · {g.size} · by {g.generatedBy}
                </div>
              </div>
              <DownloadDocButton documentId={g.id} filename={g.filename} />
              <Link
                to="/projects/$id/edit/$docId"
                params={{ id: project.id, docId: g.id }}
                className="p-1.5 rounded hover:bg-accent text-muted-foreground"
                title="Edit"
              >
                <Pencil className="h-4 w-4" />
              </Link>
              {/* No Regenerate control: a document record keeps no manifest,
                  source version or row index, so there is nothing to re-run it
                  from. Re-generate the batch from Document Mapping instead. */}
              <RowDeleteButton
                label="Delete document"
                title={`Delete "${g.filename}"?`}
                description="This permanently deletes the document, every version of it, and the rendered file on disk. This cannot be undone."
                confirmLabel="Delete document"
                successMessage="Document deleted"
                errorMessage="Couldn't delete the document"
                // The server refuses an approved document with 409: approval is
                // where somebody put their name to the contents, and §16's
                // four-eyes rule means withdrawing that is its own recorded act.
                // Saying so here beats letting them confirm and then be refused.
                disabledReason={
                  g.status === "approved"
                    ? "Approved documents cannot be deleted. Revoke the approval first."
                    : undefined
                }
                onDelete={async () => {
                  await api.deleteDocument(g.id);
                  await loadProjectDetail(project.id);
                }}
              />
            </div>
          ))}
        </div>
      )}
    </StepCard>
  );
}

/* ------------------ Shared ------------------ */
function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  busy,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  title: string;
  description: string;
  confirmLabel: string;
  busy: boolean;
  onConfirm: () => void | Promise<void>;
}) {
  return (
    <AlertDialog open={open} onOpenChange={(v) => { if (!busy) onOpenChange(v); }}>
      <AlertDialogContent className="bg-surface border-border">
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{description}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            disabled={busy}
            // Radix closes on action click; the dialog has to stay up while the
            // request is in flight so the disabled state is visible and a second
            // click can't fire it. It closes when the caller's promise settles.
            onClick={(e) => { e.preventDefault(); void onConfirm(); }}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            {busy ? "Working…" : confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** Trash icon + confirmation for one row. `onDelete` should perform the request
 *  and refresh; it throws on failure and this reports it. */
function CompileTemplateButton({ templateId, projectId }: { templateId: string; projectId: string }) {
  const loadProjectDetail = useStore((s) => s.loadProjectDetail);
  const [busy, setBusy] = useState(false);
  const { newToken, stages, failed } = useCompileProgress(busy);
  const run = async () => {
    const progressToken = newToken();
    setBusy(true);
    // No toast up front and no time estimate: an uncoloured template goes to a
    // model and can take a couple of minutes, while a colour-coded one is read
    // by the rules almost instantly. Promising a duration we cannot predict is
    // worse than a spinner that plainly means "working".
    try {
      await api.compileManifest(templateId, { progressToken });
      await loadProjectDetail(projectId);
      toast.success("Template compiled", {
        description: "Download the data template to get a spreadsheet with the right columns.",
      });
    } catch (e: any) {
      toast.error("Could not compile this template", { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col items-end gap-2">
      <button
        onClick={run}
        disabled={busy}
        title="Read this template and work out what data it needs"
        className="h-8 px-3 rounded-lg bg-gradient-brand text-white text-xs inline-flex items-center gap-1.5 hover:opacity-90 disabled:opacity-60"
      >
        {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <WandIcon className="h-3.5 w-3.5" />}
        {busy ? "Reading…" : "Compile"}
      </button>
      {/* Only while it runs. A finished compile is described by the row itself
          -- "Compiled · 25 fields, 5 conditions" -- and leaving the stage list
          behind would say the same thing twice. */}
      {busy && (
        <div className="w-full min-w-[22rem]">
          <CompileProgressList stages={stages} failed={failed} />
        </div>
      )}
    </div>
  );
}


function TemplateDataButton({ manifestId }: { manifestId: string }) {
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      const { url, filename } = await api.sourceTemplate(manifestId);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      toast.success("Data template downloaded", {
        description: "Fill it in, then upload it under Sources — its columns already match this template.",
      });
    } catch (e: any) {
      toast.error("Could not build the data template", { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <button
      onClick={run}
      disabled={busy}
      title="Download a spreadsheet with this template's columns already named"
      className="h-8 px-3 rounded-lg border border-border text-xs inline-flex items-center gap-1.5 hover:bg-accent disabled:opacity-50"
    >
      {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
      Data template
    </button>
  );
}


function RowDeleteButton({
  label,
  title,
  description,
  confirmLabel,
  successMessage,
  errorMessage,
  onDelete,
  disabledReason,
}: {
  label: string;
  title: string;
  description: string;
  confirmLabel: string;
  successMessage: string;
  errorMessage: string;
  onDelete: () => Promise<void>;
  /** When set, the control is inert and this says why. Better than letting the
   *  user confirm a destructive dialog only to be refused by the server. */
  disabledReason?: string;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      await onDelete();
      toast.success(successMessage);
      setOpen(false);
    } catch (e: any) {
      toast.error(errorMessage, { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button
        onClick={() => setOpen(true)}
        disabled={busy || !!disabledReason}
        className="p-1.5 rounded hover:bg-accent text-muted-foreground hover:text-destructive disabled:opacity-40 disabled:cursor-not-allowed"
        title={disabledReason ?? label}
        aria-label={label}
      >
        <Trash2 className="h-4 w-4" />
      </button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        busy={busy}
        title={title}
        description={description}
        confirmLabel={confirmLabel}
        onConfirm={run}
      />
    </>
  );
}

function EmptyState({
  illustration,
  title,
  subtitle,
  action,
}: {
  illustration: React.ReactNode;
  title: string;
  subtitle: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-6 text-center">
      <div className="mb-4">{illustration}</div>
      <div className="font-semibold">{title}</div>
      <div className="text-sm text-muted-foreground max-w-sm mt-1">{subtitle}</div>
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

function UploadDialog({
  open,
  onOpenChange,
  title,
  accept,
  onUpload,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  title: string;
  accept: string;
  onUpload: (file: File) => void;
}) {
  const [dragging, setDragging] = useState(false);
  const handle = (files: FileList | null) => {
    if (!files || !files[0]) return;
    onUpload(files[0]);
    onOpenChange(false);
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="bg-surface border-border">
        <DialogHeader><DialogTitle>{title}</DialogTitle></DialogHeader>
        <label
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => { e.preventDefault(); setDragging(false); handle(e.dataTransfer.files); }}
          className={cn(
            "flex flex-col items-center justify-center rounded-lg border-2 border-dashed p-10 cursor-pointer transition-colors",
            dragging ? "border-brand bg-brand/5" : "border-border hover:border-border-strong",
          )}
        >
          <UploadCloud className="h-8 w-8 text-muted-foreground mb-2" />
          <div className="text-sm font-medium">Drag and drop your file here</div>
          <div className="text-xs text-muted-foreground mt-1">or click to browse ({accept})</div>
          <input type="file" accept={accept} className="hidden" onChange={(e) => handle(e.target.files)} />
        </label>
      </DialogContent>
    </Dialog>
  );
}

/* ------------------ Illustrations ------------------ */
function TemplateBox() {
  return (
    <svg viewBox="0 0 160 130" className="w-40 h-32">
      <defs>
        <linearGradient id="tb" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="oklch(0.66 0.19 268)" /><stop offset="1" stopColor="oklch(0.6 0.22 300)" />
        </linearGradient>
      </defs>
      <rect x="15" y="70" width="130" height="10" fill="oklch(0.28 0.04 275)" opacity="0.6" />
      <rect x="20" y="82" width="120" height="8" fill="oklch(0.28 0.04 275)" opacity="0.5" />
      <rect x="25" y="93" width="110" height="6" fill="oklch(0.28 0.04 275)" opacity="0.4" />
      <rect x="45" y="20" width="60" height="50" rx="4" fill="url(#tb)" opacity="0.9" />
      <path d="M110 30 L130 20 L130 55 L110 65 Z" fill="oklch(0.65 0.22 300)" opacity="0.8" />
      <path d="M75 5 L75 20 M65 12 L75 20 L85 12" stroke="url(#tb)" strokeWidth="2" fill="none" />
    </svg>
  );
}
function SourceBox() {
  return (
    <svg viewBox="0 0 160 130" className="w-40 h-32">
      <defs>
        <linearGradient id="sb" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="oklch(0.65 0.22 300)" /><stop offset="1" stopColor="oklch(0.66 0.19 268)" />
        </linearGradient>
      </defs>
      <path d="M45 40 L80 25 L115 40 L80 55 Z" fill="url(#sb)" opacity="0.8" />
      <path d="M45 40 L45 85 L80 100 L80 55 Z" fill="oklch(0.4 0.05 275)" />
      <path d="M115 40 L115 85 L80 100 L80 55 Z" fill="oklch(0.5 0.08 280)" />
      <path d="M25 70 L45 65 M25 80 L45 75" stroke="oklch(0.66 0.19 268)" strokeWidth="2" markerEnd="url(#arr)" />
    </svg>
  );
}
function DraftBox() {
  return (
    <svg viewBox="0 0 160 130" className="w-40 h-32">
      <defs>
        <linearGradient id="db" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="oklch(0.66 0.19 268)" /><stop offset="1" stopColor="oklch(0.6 0.22 300)" />
        </linearGradient>
      </defs>
      <rect x="30" y="30" width="90" height="80" rx="4" fill="url(#db)" opacity="0.7" transform="skewY(-5)" />
      <rect x="40" y="40" width="90" height="80" rx="4" fill="oklch(0.4 0.05 275)" transform="skewY(-5)" />
      <rect x="50" y="50" width="90" height="80" rx="4" fill="oklch(0.55 0.08 285)" transform="skewY(-5)" />
    </svg>
  );
}
function NetworkNodes() {
  return (
    <svg viewBox="0 0 160 130" className="w-40 h-32">
      <defs>
        <linearGradient id="nn" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="oklch(0.66 0.19 268)" /><stop offset="1" stopColor="oklch(0.6 0.22 300)" />
        </linearGradient>
      </defs>
      <path d="M80 30 L40 80 M80 30 L80 80 M80 30 L120 80" stroke="url(#nn)" strokeWidth="2" />
      <rect x="70" y="20" width="20" height="20" fill="url(#nn)" />
      <rect x="30" y="70" width="20" height="20" fill="oklch(0.5 0.05 275)" />
      <rect x="70" y="70" width="20" height="20" fill="oklch(0.5 0.05 275)" />
      <rect x="110" y="70" width="20" height="20" fill="oklch(0.5 0.05 275)" />
    </svg>
  );
}
