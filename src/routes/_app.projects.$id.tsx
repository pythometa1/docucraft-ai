import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { useStore } from "@/lib/store";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { CompileProgressList, useCompileProgress } from "@/components/compile-progress";
import { DocumentMapping } from "@/components/document-mapping";
import { BatchProgressPanel, useBatchWatch, type BatchWatch } from "@/components/batch-progress";
import { motion } from "framer-motion";
import { EASE_OUT, FadeIn, Stagger, StaggerItem, SwapIn, useReducedMotionFlag } from "@/components/motion";
import { ErrorBanner } from "@/components/error-banner";
import { ClinicalStudio, hasClinicalService } from "@/components/clinical-studio";
import { CmcWorkspace } from "@/components/cmc-workspace";
import { CsrWorkspace } from "@/components/csr-workspace";
import { SafetyWorkspace } from "@/components/safety-workspace";
import { InvoiceStudio } from "@/components/invoice-studio";
import { PolishedEmpty, SkeletonBar, StageSkeleton } from "@/components/skeletons";
import { plainly } from "@/components/processing-banner";
import { SETTABLE_WORKFLOW, WORKFLOW_LABELS } from "@/lib/types";
import type { SettableWorkflowStatus, WorkflowStatus } from "@/lib/types";
import {
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
  Trash2,
  Share2,
  Archive,
  Check,
  Wand2 as WandIcon,
  Loader2,
  MessageSquare,
  AlertTriangle,
} from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
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
import { BulkSelectBar, SelectBox, useSelection } from "@/components/bulk-select";
import { ReasonDialog, StatusChip } from "@/components/review-bar";

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
  { key: "mapping2", n: 3, title: "Document Mapping", short: "Fill", icon: Network, hint: "Map the columns, then generate" },
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
  // Held here, not inside Document Mapping. It used to live in that component's
  // own state, which made it the only route to the batch's error, its per-row
  // failures and its archive -- and a stage change unmounted it and lost all
  // three. Rows that fail outright never produce a document, so a failed row
  // then left no trace anywhere in the product.
  const watch = useBatchWatch();

  useEffect(() => {
    // Cleared first: navigating from a project that failed to load to one that
    // loads fine otherwise leaves the previous project's error on screen for
    // good, because nothing else ever resets it.
    setLoadError(null);
    // And the stage choice is reset, not carried across. Stage 3 of the project
    // you just left is not a sensible place to open the project you just opened.
    setActive(null);
    loadProjectDetail(id).catch((e: any) => setLoadError(e?.message ?? String(e)));
  }, [id]);

  const done: Record<StageKey, boolean> = {
    // Uploading is not the same as being ready, and neither is *attempting* to
    // read. A failed compile writes a manifest row too, so `!!t.manifestId`
    // alone counted a template nothing could read as a finished stage -- which
    // hid the failure behind a tick and moved the user past the retry.
    template: (project?.templates ?? []).some(
      (t: any) => !!t.manifestId && t.manifestStatus !== "failed"),
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

  // Pinned once, when the project first arrives, and left alone afterwards.
  //
  // `active ?? firstIncomplete` on its own is not a default -- it is a live
  // expression, recomputed on every render. So the moment a compile wrote a
  // manifest, `done.template` flipped, `firstIncomplete` moved on, and the panel
  // swapped from Template to Sources underneath the user: no navigation event,
  // no toast, nothing to dismiss, and the button they had just pressed gone from
  // the screen. Worse when the compile *failed*, which also writes a manifest
  // row: the stage that would have shown them why disappeared.
  useEffect(() => {
    if (project && active === null) setActive(firstIncomplete);
    // Keyed on the project alone. Adding `firstIncomplete` here would restore
    // the original bug, because that is the value that moves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.id]);

  if (loadError) {
    return (
      <div className="p-6 md:p-8 max-w-2xl mx-auto space-y-4">
        {/* The server's own words are kept verbatim rather than folded into the
            sentence: "project is archived" and "connection refused" need
            different things doing about them, and a single friendly paraphrase
            of both is a sentence nobody can act on. */}
        <ErrorBanner
          title="This project couldn't be opened"
          message="It may have been deleted or archived, or the server may not have answered in time."
          detail={loadError}
        />
        <Link to="/dashboard" className="text-sm text-brand hover:underline">Back to dashboard</Link>
      </div>
    );
  }
  if (!project) return <ProjectSkeleton />;

  // An Invoice project is the invoice service's home; a Clinical project --
  // any of its four document types -- is the clinical service's. The
  // spreadsheet-driven four-stage pipeline gives way to describe -> template
  // -> values -> numbered document, scoped to this project. Same breadcrumb,
  // same header, different machine underneath. documentType wins for Invoice:
  // the taxonomy keeps the two disjoint, but the tiebreak is stated anyway.
  const isInvoiceProject = project.documentType === "Invoice";
  // The CSR module claims the "Clinical Study Report" document type; the
  // template studio keeps the other clinical types (amendments, consent
  // forms, brochures), which really are one-template documents.
  const isCsrProject =
    project.function === "Clinical" && project.documentType === "Clinical Study Report";
  const isClinicalProject =
    !isInvoiceProject && !isCsrProject
    && project.function === "Clinical" && hasClinicalService(project.documentType);
  // A Quality-CMC project is the quality dossier's home, whatever its
  // document type: 3.2.S, 3.2.P and an APQR are deliverables inside one
  // dossier rather than three different screens.
  const isCmcProject = project.function === "Quality-CMC";
  // A Safety project is the pharmacovigilance module's home whatever its
  // document type, for the same reason: a PBRER and a DSUR are reporting
  // intervals inside one product's safety profile rather than two screens.
  const isSafetyProject = project.function === "Safety";
  const hasVerticalStudio =
    isInvoiceProject || isCsrProject || isClinicalProject || isCmcProject
    || isSafetyProject;

  const activeKey = active ?? firstIncomplete;
  const activeIdx = STAGES.findIndex((s) => s.key === activeKey);
  const activeStage = STAGES[activeIdx];
  const completedCount = Object.values(done).filter(Boolean).length;
  // A ratio rather than a percentage, because it is fed to `scaleX` rather than
  // to `width` -- see the progress bar below.
  const progressRatio = completedCount / STAGES.length;

  return (
    <div className="p-6 md:p-8 max-w-7xl mx-auto space-y-6">
      {/* Breadcrumb */}
      <div className="text-sm text-muted-foreground flex items-center gap-2">
        <Link to="/dashboard" className="hover:text-foreground">Projects</Link>
        <ChevronRight className="h-3.5 w-3.5" />
        <span className="text-foreground">{project.name}</span>
      </div>

      {/* Title + meta strip */}
      <FadeIn className="rounded-2xl surface-raised overflow-hidden">
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
            {!hasVerticalStudio && (
              <div className="text-right pr-3 border-r border-border">
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Progress</div>
                <div className="text-sm font-semibold">{completedCount}/{STAGES.length} stages</div>
              </div>
            )}
            <ShareButton />
            <ProjectActions project={project} />
          </div>
        </div>
        {/* Progress bar.
            `scaleX` from a left origin, not `width`. A width transition is laid
            out and painted again on every one of its thirty frames; a transform
            is composited, so the same movement costs the browser nothing. The
            fill is full width and scaled down to the ratio, which reads
            identically at 0 (nothing but the track) and at 1 (the full strip).

            The sheen still travels across the filled part only -- the fill's own
            `overflow: hidden` is what clips it -- but it sits inside a
            counter-scaled wrapper, because the fill's `scaleX` would otherwise
            squash the highlight horizontally along with the bar and turn a soft
            sweep into a hard line. Both scales carry the same transition so the
            two stay in step while the bar moves.

            And it sweeps once, keyed on the count, rather than looping: this
            strip measures how many stages are done, so the honest moment for it
            to catch the light is the moment one of them is. */}
        {!hasVerticalStudio && (
        <div className="h-1 bg-muted">
          <div
            className="sheen h-full w-full bg-gradient-brand transition-transform duration-500"
            style={{ transformOrigin: "left", transform: `scaleX(${progressRatio})` }}
          >
            {completedCount > 0 && (
              <span
                aria-hidden
                className="absolute inset-0 transition-transform duration-500"
                style={{ transformOrigin: "left", transform: `scaleX(${1 / progressRatio})` }}
              >
                <span key={completedCount} className="sheen-layer sheen-layer-once" />
              </span>
            )}
          </div>
        </div>
        )}
      </FadeIn>

      {isInvoiceProject ? (
        <InvoiceStudio project={project} />
      ) : isCsrProject ? (
        <CsrWorkspace project={project} />
      ) : isCmcProject ? (
        <CmcWorkspace project={project} />
      ) : isSafetyProject ? (
        <SafetyWorkspace project={project} />
      ) : isClinicalProject ? (
        <ClinicalStudio project={project} />
      ) : (
        <>
      {/* Pipeline rail */}
      <PipelineRail stages={STAGES} done={done} active={activeKey} onSelect={setActive} />

      {/* Active stage panel */}
      <div className="rounded-2xl surface-raised">
        <div className="flex items-center gap-4 p-6 border-b border-border">
          <div className={cn(
            "h-11 w-11 rounded-xl flex items-center justify-center border transition-colors duration-200",
            // Green here is the engine's verdict on a stage, not the product's
            // own colour, so it comes from the ai-* set that every other verdict
            // in the app is drawn from. Brand stays brand: "you are here".
            done[activeKey]
              ? "bg-ai-confident/10 text-ai-confident border-ai-confident/30"
              : "bg-brand/10 text-brand border-brand/30",
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
        {/* Keyed on the stage: the panel swaps its contents in place, so there
            is no mount for an entrance to hang off without this. */}
        <SwapIn k={activeKey} className="p-6">
          {activeKey === "template" && <Step1Template project={project} />}
          {activeKey === "source" && <Step2Source project={project} />}
          {activeKey === "drafts" && <StageDocuments project={project} watch={watch} />}
          {activeKey === "mapping2" && (
            <DocumentMapping
              project={project}
              watch={watch}
              onGenerating={() => setActive("drafts")}
            />
          )}
        </SwapIn>
      </div>
        </>
      )}
    </div>
  );
}

/**
 * The project screen's own shape, held while it loads.
 *
 * Sized to the real thing -- the same page padding, the same three stacked
 * cards, one placeholder per stage in the rail -- so the header and the panel
 * land where their outlines already were instead of the whole page jumping down
 * when the request returns. A spinner centred in an empty page guarantees the
 * jump, and tells the reader nothing about what is arriving.
 */
function ProjectSkeleton() {
  return (
    <div className="p-6 md:p-8 max-w-7xl mx-auto space-y-6" role="status" aria-live="polite">
      <span className="sr-only">Loading project…</span>
      <SkeletonBar className="h-3 w-56" />
      <div className="rounded-2xl surface-raised p-6 space-y-3">
        <SkeletonBar className="h-2.5 w-64" />
        <SkeletonBar className="h-7 w-80 max-w-full" />
        <SkeletonBar className="h-3 w-full max-w-xl" />
      </div>
      <div
        className="rounded-2xl surface-raised p-4 grid gap-3"
        style={{ gridTemplateColumns: `repeat(${STAGES.length}, minmax(0, 1fr))` }}
      >
        {STAGES.map((s) => (
          <div key={s.key} className="flex flex-col items-center gap-2">
            <SkeletonBar className="h-10 w-10 rounded-full" />
            <SkeletonBar className="h-3 w-20" />
            <SkeletonBar className="h-2 w-12" />
          </div>
        ))}
      </div>
      <div className="rounded-2xl surface-raised p-6">
        <StageSkeleton />
      </div>
    </div>
  );
}

/** The dot in front of a one-line verdict. It inherits `currentColor`, so a row
 *  names its state token once and the mark can never drift away from the words
 *  beside it. */
function StateDot() {
  return (
    <span
      aria-hidden
      className="mr-1.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-current align-middle"
    />
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
      className="h-9 px-3 rounded-lg surface-raised hover:bg-accent text-sm inline-flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
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
            className="h-9 w-9 rounded-lg surface-raised hover:bg-accent flex items-center justify-center disabled:opacity-50 disabled:cursor-not-allowed"
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
          <DialogHeader>
            <DialogTitle>Rename project</DialogTitle>
            {/* Radix warns without one, and a screen reader announces a dialog
                with a title and no description as a title alone. */}
            <DialogDescription>
              The new name is what appears in the project list and on generated filenames.
            </DialogDescription>
          </DialogHeader>
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
              className="bg-gradient-brand text-white shadow-lg shadow-brand/25 transition-all hover:opacity-95 hover:shadow-brand/40"
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
        description="This permanently removes the project along with its templates, sources, everything read from them and the generated documents. This cannot be undone."
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
    <div className="rounded-2xl surface-raised p-4">
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
          // A completed stage you are standing in keeps its number. The rail is
          // a position indicator as well as a progress one, and once every stage
          // is done a row of four identical ticks no longer says where you are.
          const settled = isDone && !isActive;
          return (
            <div key={s.key} className="relative">
              {/* Connector line */}
              {i < stages.length - 1 && (
                <div className={cn(
                  "hidden md:block absolute top-5 left-[calc(50%+22px)] right-[-12px] h-px transition-colors duration-300",
                  done[stages[i + 1].key] || (isDone && !done[stages[i + 1].key])
                    ? "bg-ai-confident/50"
                    : "bg-border",
                )} />
              )}
              <button
                onClick={() => onSelect(s.key)}
                aria-current={isActive ? "step" : undefined}
                className="w-full flex flex-col items-center text-center gap-2 group"
              >
                <div className={cn(
                  "h-10 w-10 rounded-full flex items-center justify-center border-2 font-mono text-sm font-semibold relative z-10",
                  // Named properties rather than `transition-all`, which also
                  // animated the border *width*: the ring thickened a beat after
                  // its colour landed and the circle read as wobbling.
                  "transition-[transform,color,background-color,border-color,box-shadow] duration-200 ease-out",
                  // `glow-brand` is the coloured ring and the halo in one, both
                  // as box-shadows, so lighting the current stage never moves it
                  // or its neighbours by a pixel.
                  isActive
                    ? "bg-gradient-brand text-white border-transparent glow-brand scale-110"
                    : settled
                    ? "bg-ai-confident/10 text-ai-confident border-ai-confident/45 group-hover:border-ai-confident/70"
                    : "bg-background text-muted-foreground border-border group-hover:border-border-strong group-hover:text-foreground",
                )}>
                  {/* Keyed on which mark is showing, so the tick settles in on
                      the render where the stage completes rather than swapping
                      between two frames with nothing to mark the moment. */}
                  <SwapIn k={settled ? "check" : "number"}>
                    {settled ? <Check className="h-4 w-4" /> : s.n}
                  </SwapIn>
                </div>
                <div className="min-w-0">
                  <div className={cn(
                    "text-sm font-semibold truncate transition-colors duration-200",
                    isActive || isDone ? "text-foreground" : "text-muted-foreground",
                  )}>{s.title}</div>
                  <div className={cn(
                    "text-[10px] uppercase tracking-wider font-mono truncate transition-colors duration-200",
                    settled ? "text-ai-confident" : isActive ? "text-brand" : "text-muted-foreground",
                  )}>
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
          subtitle="Import a template file and it is read straight away, so the project knows what data the letter needs."
          action={
            <Button onClick={() => setUploadOpen(true)} className="bg-gradient-brand text-white shadow-lg shadow-brand/25 transition-all hover:opacity-95 hover:shadow-brand/40">
              <Upload className="h-4 w-4 mr-1.5" /> Import template file
            </Button>
          }
        />
      ) : (
        /* No select-all here, and none on Sources either.
           Both lists are short, both are the input to everything downstream, and
           a mis-ticked row on either is a template or a spreadsheet somebody has
           to go and find again. The per-row control below is the whole delete
           story for these two; documents and projects keep theirs, where the
           lists are long enough for one-at-a-time to be the wrong tool. */
        <Stagger className="space-y-2">
          {project.templates.map((t: any, i: number) => (
            <StaggerItem key={t.id} index={i}>
              <div className="rounded-lg border border-border bg-background/40 p-3 transition-colors hover:border-border-strong">
                <div className="flex items-center gap-3">
                  <FileText className="h-5 w-5 text-info shrink-0" />
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-sm">{t.name}</div>
                    <div className="text-xs text-muted-foreground">
                      {t.size} · uploaded {t.uploadedAt} by {t.uploadedBy}
                    </div>
                    {/* Compiling is a fact about the template, so it is stated on
                        the template. A failed compile writes a manifest row too,
                        so the status decides the wording -- reading only
                        `manifestId` rendered a failure as "Compiled · 0 fields, 0
                        conditions" in green, which is what a clean compile of a
                        template with no placeholders looks like and nothing like
                        what happened.

                        The three verdicts are drawn in the ai-* tokens rather
                        than raw amber and emerald, so "this is fine" and "this
                        stopped" are the same two colours here as they are on
                        every other screen. */}
                    <div className="mt-1 text-xs">
                      {t.manifestId && t.manifestStatus === "failed" ? (
                        <span className="text-ai-blocked">
                          <StateDot />
                          Could not read this template — nothing was mapped.
                          {t.compileError ? ` ${plainly(String(t.compileError))}` : ""}
                        </span>
                      ) : t.manifestId ? (
                        <span className={t.unfillableCount ? "text-ai-uncertain" : "text-ai-confident"}>
                          <StateDot />
                          Read · {t.fieldCount} fields, {t.conditionCount} conditions
                          {t.unfillableCount
                            ? ` · ${t.unfillableCount} placeholder${t.unfillableCount === 1 ? "" : "s"} nothing will fill`
                            : ""}
                        </span>
                      ) : (
                        <span className="text-ai-uncertain"><StateDot />Not read yet</span>
                      )}
                    </div>
                  </div>
                  {t.manifestId && t.manifestStatus !== "failed" && <TemplateDataButton manifestId={t.manifestId} />}
                  {/* Only as a retry. Reading happens at upload now, so a Compile
                      button on a template that has already been read is an offer
                      to pay for a model call to learn what is already known. */}
                  {(!t.manifestId || t.manifestStatus === "failed") && (
                    <CompileTemplateButton templateId={t.id} projectId={project.id} />
                  )}
                  <EditTemplateButton
                    templateId={t.id}
                    projectId={project.id}
                    blueprintId={t.blueprintId}
                    readable={Boolean(t.manifestId) && t.manifestStatus !== "failed"}
                  />
                  <RowDeleteButton
                    label="Delete template"
                    title={`Delete "${t.name}"?`}
                    description="The template is removed from this project. What was already read from it, and any documents already generated, are kept."
                    confirmLabel="Delete template"
                    successMessage="Template deleted"
                    errorMessage="Couldn't delete the template"
                    onDelete={async () => {
                      await api.deleteTemplate(t.id);
                      await loadProjectDetail(project.id);
                    }}
                  />
                </div>
                <UnfillablePanel template={t} projectId={project.id} />
              </div>
            </StaggerItem>
          ))}
          <Button variant="outline" onClick={() => setUploadOpen(true)}>
            <Plus className="h-4 w-4 mr-1.5" /> Add another template
          </Button>
        </Stagger>
      )}
      <TemplateUploadDialog
        open={uploadOpen}
        onOpenChange={setUploadOpen}
        projectId={project.id}
      />
    </StepCard>
  );
}

/**
 * The placeholders this template will not fill, named on the template's own row.
 *
 * Every one of these is a document that will come back blocked with "Leftover
 * placeholder brackets": the fill engine writes the value into the run it found
 * the slot in, and for these there is no slot, so the literal `<Pay Rate
 * Monthly>` survives into the letter and the QA gate refuses it.
 *
 * That was only discoverable by running a batch. Three canary rows fail, the run
 * stops, and the reader is on the Documents stage looking at an error about a
 * template they uploaded four steps ago -- with a spreadsheet in between that had
 * nothing to do with it. The check runs at compile time and the compile now runs
 * at upload, so the fact is available here, which is both the earliest moment and
 * the only one where the reader is already looking at the thing they have to
 * change.
 *
 * The two codes need different advice and are not flattened together:
 * `uncovered_placeholder` is a slot a field could still claim, and
 * `W-SPLIT-PLACEHOLDER` is one Word has broken across runs, where no field *can*
 * be attached and the fix is in the document.
 */
function UnfillablePanel({ template, projectId }: { template: any; projectId: string }) {
  const [open, setOpen] = useState(false);
  const items: any[] = template.unfillable ?? [];
  if (!items.length) return null;

  const split = items.filter((w) => w.code === "W-SPLIT-PLACEHOLDER");
  const shown = open ? items : items.slice(0, 3);

  return (
    <div className="mt-2.5 rounded-lg border border-ai-uncertain/30 bg-ai-uncertain/5 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="flex items-center gap-1.5 text-xs font-medium text-ai-uncertain">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          {template.unfillableCount} placeholder{template.unfillableCount === 1 ? "" : "s"} nothing
          will fill
        </p>
        <Link
          to="/templates/$blueprintId"
          params={{ blueprintId: template.blueprintId ?? "" }}
          search={{ project: projectId, template: template.id }}
          className={cn(
            "text-xs font-medium text-ai-uncertain hover:underline",
            !template.blueprintId && "pointer-events-none opacity-40",
          )}
        >
          Fix in the template →
        </Link>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        Generating now produces documents that fail their checks with “Leftover placeholder
        brackets” — the literal text stays where the value should be.
        {split.length > 0 && (
          <>
            {" "}
            {split.length === items.length ? "These are" : `${split.length} of these are`} split
            across runs by Word, so no field can be attached: retype the placeholder in one go and
            read the template again.
          </>
        )}
      </p>
      <ul className="mt-2 space-y-1">
        {shown.map((w, i) => (
          <li key={i} className="flex flex-wrap items-baseline gap-x-2 text-xs">
            <code className="rounded bg-ai-uncertain/10 px-1.5 py-0.5 font-mono text-[11px] text-ai-uncertain">
              {w.placeholder || "unnamed"}
            </code>
            {w.paragraph_index != null && (
              <span className="text-muted-foreground">paragraph {w.paragraph_index}</span>
            )}
            {w.code === "W-SPLIT-PLACEHOLDER" && (
              <span className="text-muted-foreground opacity-70">· split across runs</span>
            )}
          </li>
        ))}
      </ul>
      {items.length > 3 && (
        <button
          onClick={() => setOpen((v) => !v)}
          className="mt-1.5 text-xs text-muted-foreground underline decoration-dotted hover:text-foreground"
        >
          {open ? "Show fewer" : `Show all ${template.unfillableCount}`}
        </button>
      )}
    </div>
  );
}

/**
 * Upload a template and watch it being read.
 *
 * One dialog for what used to be two steps. It stays open while the compile
 * runs, because that is the part worth watching: a template read by the colour
 * rules costs nothing and finishes instantly, while one handed to a model costs
 * real money and takes minutes, and a single spinner cannot tell those apart --
 * or tell either from a request that has hung.
 *
 * A compile that fails does not undo the upload. The file is stored, the row is
 * on the screen, and the reason is here with a retry next to it.
 */
function TemplateUploadDialog({ open, onOpenChange, projectId }: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  projectId: string;
}) {
  const add = useStore((s) => s.addTemplate);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { newToken, stages, failed } = useCompileProgress(busy != null);

  const handle = async (files: FileList | null) => {
    const file = files?.[0];
    if (!file || busy) return;
    setError(null);
    // Minted before the request, not after: the server writes progress against
    // this token while it works, and an id the client learns from the response
    // is an id it learns when there is nothing left to watch.
    const token = newToken();
    setBusy(file.name);
    try {
      await add(projectId, file, token);
      toast.success("Template read", {
        description: `${file.name} — the project now knows what data it needs.`,
      });
      onOpenChange(false);
    } catch (e: any) {
      setError(
        e?.code === "LLM_NOT_CONFIGURED"
          ? "Reading a template needs a language model, and none is configured. The file was uploaded and can be read once one is."
          : e?.message ?? String(e),
      );
    } finally {
      setBusy(null);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!busy) { setError(null); onOpenChange(v); } }}>
      <DialogContent className="bg-surface border-border">
        <DialogHeader>
          <DialogTitle>Upload template</DialogTitle>
          <DialogDescription>
            It is read as soon as it lands — placeholders, author instructions and conditional
            sections are worked out for you.
          </DialogDescription>
        </DialogHeader>

        {busy ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-sm">
              <Loader2 className="h-4 w-4 animate-spin text-ai-active" />
              <span className="truncate font-medium">{busy}</span>
            </div>
            {/* No time estimate. An uncoloured template goes to a model and can
                take a couple of minutes; a colour-coded one is read by the rules
                almost instantly. Promising a duration we cannot predict is worse
                than naming the stage that is running.

                And no `result`: on this path the compile is issued inside
                `addTemplate`, which uploads, compiles and reloads as one action
                and answers `void`. The body never reaches this component, so the
                panel narrates the stages and stops -- which is the honest end of
                what this screen knows. Reaching around the store to re-request
                it would be a second compile of the same template. */}
            <CompileProgressList stages={stages} failed={failed} />
          </div>
        ) : (
          <label
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => { e.preventDefault(); setDragging(false); void handle(e.dataTransfer.files); }}
            className={cn(
              "flex flex-col items-center justify-center rounded-lg border-2 border-dashed p-10 cursor-pointer transition-colors",
              dragging ? "border-brand bg-brand/5" : "border-border hover:border-border-strong",
            )}
          >
            <UploadCloud className="h-8 w-8 text-muted-foreground mb-2" />
            <div className="text-sm font-medium">Drag and drop your file here</div>
            <div className="text-xs text-muted-foreground mt-1">or click to browse (.docx, .dotx)</div>
            <input
              type="file" accept=".docx,.dotx" className="hidden"
              onChange={(e) => void handle(e.target.files)}
            />
          </label>
        )}

        {/* `plainly` runs over the server's own sentence on its way to the
            screen: the stage labels it is built from are read by the transcript
            and the audit row as well, so they are reworded here rather than at
            the source, where renaming them would have changed a record somebody
            may have to defend later. */}
        {error && (
          <ErrorBanner
            title="Couldn't read this template"
            message={`${plainly(error)} The file itself was uploaded — use “Read again” on its row to try once more.`}
          />
        )}
      </DialogContent>
    </Dialog>
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
            <Button onClick={() => setUploadOpen(true)} className="bg-gradient-brand text-white shadow-lg shadow-brand/25 transition-all hover:opacity-95 hover:shadow-brand/40">
              <Upload className="h-4 w-4 mr-1.5" /> Import source files
            </Button>
          }
        />
      ) : (
        <div className="space-y-3">
          <Stagger className="grid md:grid-cols-2 gap-3">
            {project.sources.map((s: any, i: number) => (
              <StaggerItem key={s.id} index={i} className="h-full">
                <div className="h-full rounded-lg border border-border bg-background/40 p-4 transition-colors hover:border-border-strong">
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
              </StaggerItem>
            ))}
            <button
              onClick={() => setUploadOpen(true)}
              className="rounded-lg border border-dashed border-border p-4 text-sm text-muted-foreground transition-colors hover:text-foreground hover:border-border-strong"
            >
              <Plus className="h-4 w-4 inline mr-1.5" /> Add another source
            </button>
          </Stagger>
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

/* A row here carries the *document* id -- the store drops current_version_id
   when it maps the API response -- so resolve the current version before asking
   for a file. */
async function saveBlob(url: string, filename: string) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/**
 * A padlock that engages, drawn rather than swapped between two glyphs.
 *
 * The shackle has to retract into the body for the gesture to read as "this just
 * locked" rather than as a picture of a lock, and no pair of static icons can do
 * that. It is one `scaleY` on one path from the shackle's own base, so the whole
 * thing is a composited transform and nothing is laid out again.
 *
 * It plays on mount, which is when the document arrives on screen carrying the
 * QA verdict that locked it -- during a batch that is one row at a time, as each
 * one lands.
 */
function EngagingLock({ className }: { className?: string }) {
  const reduced = useReducedMotionFlag();
  const settle = reduced ? { duration: 0 } : { duration: 0.25, ease: EASE_OUT };
  return (
    <svg viewBox="0 0 16 16" className={cn("h-4 w-4", className)} aria-hidden focusable="false">
      <motion.path
        d="M5.3 7.6V5.3a2.7 2.7 0 0 1 5.4 0v2.3"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        style={{ transformBox: "fill-box", transformOrigin: "bottom" }}
        initial={reduced ? false : { scaleY: 1.6, y: -1.6 }}
        animate={{ scaleY: 1, y: 0 }}
        transition={settle}
      />
      <motion.rect
        x="3" y="7.3" width="10" height="6.5" rx="1.6"
        fill="currentColor"
        style={{ transformBox: "fill-box", transformOrigin: "bottom" }}
        initial={reduced ? false : { scaleY: 0.8, opacity: 0.65 }}
        animate={{ scaleY: 1, opacity: 1 }}
        transition={settle}
      />
    </svg>
  );
}

/** `allowed` mirrors the server's own gate. Disabling with a reason beats letting
 *  somebody press it and read a 409 in a toast.
 *
 *  `status === "blocked"` is the one refusal that gets its own treatment. It is
 *  not "not approved yet" -- it is the QA gate having found something wrong with
 *  the file itself, which is the only one of these a person has to go and fix. So
 *  the control locks visibly and stays pressable, because what it does when
 *  pressed is name the reason: `reasons` is the QA engine's own prose, and a
 *  greyed-out icon with a tooltip is where that text goes to die. */
function DownloadDocButton({ documentId, filename, allowed, status, reasons = [] }: {
  documentId: string; filename: string; allowed: boolean;
  status?: string;
  /** Why it is locked, in the engine's words. Empty is a real answer and is
   *  rendered as one -- a freshly generated blocked document has a verdict and
   *  no recorded sentence, and saying so beats inventing a sentence for it. */
  reasons?: string[];
}) {
  const [busy, setBusy] = useState<null | "docx" | "pdf">(null);
  const [open, setOpen] = useState(false);
  const lockedByQa = status === "blocked";

  async function go(format: "docx" | "pdf") {
    setBusy(format);
    setOpen(false);
    try {
      const doc = await api.getDocument(documentId);
      const versionId = doc?.current_version_id;
      if (!versionId) throw new Error("This document has no saved version to download yet.");
      const url = await api.downloadVersion(versionId, format);
      await saveBlob(url, filename.replace(/\.docx?$/i, "") + "." + format);
    } catch (e: any) {
      // The server's own message, not a generic one: "LibreOffice is not
      // installed on this host" is something the reader can act on.
      toast.error("Download failed", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="relative">
      <button
        onClick={() => { if (allowed || lockedByQa) setOpen((v) => !v); }}
        disabled={busy !== null || (!allowed && !lockedByQa)}
        className={cn(
          "rounded p-1.5 transition-colors disabled:cursor-not-allowed disabled:opacity-40",
          lockedByQa
            ? "text-ai-blocked hover:bg-ai-blocked/10"
            : "text-muted-foreground hover:bg-accent",
        )}
        title={lockedByQa
          ? reasons.length
            ? `Locked — ${plainly(String(reasons[0]))}`
            : "Locked — this document failed its checks when it was generated."
          : allowed
            ? "Download"
            : "Only approved documents can be downloaded. Approve this one first."}
        aria-label={lockedByQa ? "Download locked — why?" : "Download"}
        aria-haspopup={lockedByQa ? "dialog" : "menu"}
        aria-expanded={open}
      >
        {lockedByQa ? <EngagingLock /> : <Download className="h-4 w-4" />}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} aria-hidden />
          {lockedByQa ? (
            <div className="absolute right-0 z-20 mt-1 w-72 rounded-lg border border-ai-blocked/40 bg-popover p-3 shadow-md">
              <p className="text-[12px] font-semibold leading-snug text-ai-blocked">
                Locked by the QA checks
              </p>
              {reasons.length > 0 ? (
                <ul className="mt-2 space-y-1.5">
                  {reasons.map((r, i) => (
                    <li key={i} className="flex items-start gap-1.5 text-[11.5px] leading-relaxed text-muted-foreground">
                      <span aria-hidden className="mt-[6px] h-1 w-1 shrink-0 rounded-full bg-ai-blocked/70" />
                      {/* The checker's own sentence, scrubbed of internal
                          vocabulary on its way to the screen. */}
                      <span>{plainly(String(r))}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-2 text-[11.5px] leading-relaxed text-muted-foreground">
                  No note was recorded against this document. Regenerate the batch to see what the
                  checks object to.
                </p>
              )}
            </div>
          ) : (
            <div role="menu" className="absolute right-0 z-20 mt-1 w-40 overflow-hidden rounded-md border border-border bg-popover shadow-md">
              <button role="menuitem" onClick={() => go("docx")} className="block w-full px-3 py-2 text-left text-sm hover:bg-accent">
                Download .docx
              </button>
              <button role="menuitem" onClick={() => go("pdf")} className="block w-full px-3 py-2 text-left text-sm hover:bg-accent">
                Download .pdf
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** `documents` is already filtered to what the server will hand over; `total` is
 *  how many there are in all, so the button can say what it is leaving out
 *  instead of quietly downloading a subset. */
function DownloadAllButton({ documents, total }: { documents: any[]; total: number }) {
  const [busy, setBusy] = useState<null | "docx" | "pdf">(null);
  const [open, setOpen] = useState(false);

  async function go(format: "docx" | "pdf") {
    setBusy(format);
    setOpen(false);
    try {
      const url = await api.downloadDocuments(documents.map((d) => d.id), format);
      await saveBlob(url, `documents_${format}.zip`);
      toast.success(`${documents.length} document(s) prepared`, {
        description: "Any that could not be converted are listed in _FAILED.txt inside the archive.",
      });
    } catch (e: any) {
      toast.error("Download failed", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  // Deliberately still rendered when nothing is downloadable, disabled and
  // saying why. Hiding it left a reader who had generated forty letters with no
  // download control at all and nothing to explain its absence.
  if (!total) return null;
  const none = documents.length === 0;
  return (
    <div className="relative">
      <button
        onClick={() => { if (!none) setOpen((v) => !v); }}
        disabled={busy !== null || none}
        title={none
          ? "Only approved documents can be downloaded, and none of these are approved yet."
          : documents.length < total
            ? `${total - documents.length} of ${total} are not approved and are left out.`
            : undefined}
        className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium hover:bg-accent disabled:opacity-40 disabled:cursor-not-allowed"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <Download className="h-3.5 w-3.5" />
        {busy ? `Preparing ${busy}…` : `Download approved (${documents.length})`}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} aria-hidden />
          <div role="menu" className="absolute right-0 z-20 mt-1 w-48 overflow-hidden rounded-md border border-border bg-popover shadow-md">
            <button role="menuitem" onClick={() => go("docx")} className="block w-full px-3 py-2 text-left text-sm hover:bg-accent">
              All as .docx
            </button>
            <button role="menuitem" onClick={() => go("pdf")} className="block w-full px-3 py-2 text-left text-sm hover:bg-accent">
              All as .pdf
            </button>
          </div>
        </>
      )}
    </div>
  );
}


/** Which workflow states a reader is looking at. `all` is the default because
 *  hiding rows by default is how somebody concludes a document was never
 *  generated. */
const WORKFLOW_FILTERS: { key: WorkflowStatus | "all"; label: string }[] = [
  { key: "all", label: "All" },
  { key: "work_in_progress", label: "Work in progress" },
  { key: "completed", label: "Completed" },
  { key: "approved", label: "Approved" },
  { key: "blocked", label: "Blocked" },
  { key: "cancelled", label: "Cancelled" },
];

function StageDocuments({ project, watch }: { project: any; watch: BatchWatch }) {
  const loadProjectDetail = useStore((s) => s.loadProjectDetail);
  const count = project.generated.length;
  const [filter, setFilter] = useState<WorkflowStatus | "all">("all");

  // Documents appear as the batch produces them, so the list is reloaded while
  // one runs and once more when it stops. Without the second reload the final
  // few rows -- and every status the QA gate settled on the way out -- are
  // whatever the last poll happened to catch.
  const running = watch.running;
  const jobStatus = watch.job?.status;
  useEffect(() => {
    if (!watch.job) return;
    void loadProjectDetail(project.id);
    if (!running) return;
    const timer = setInterval(() => void loadProjectDetail(project.id), 2500);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id, running, jobStatus]);

  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const g of project.generated) out[g.workflowStatus] = (out[g.workflowStatus] ?? 0) + 1;
    return out;
  }, [project.generated]);

  /**
   * Why each blocked document is blocked, in the QA engine's own sentences,
   * keyed by the version the row wrote.
   *
   * This is the only place those sentences exist while the batch is the thing
   * that produced them. The runner writes the version with the verdict and
   * nothing else -- `status_reason` stays null until something later calls
   * `refresh_status` -- so a document that has just come off a batch has a
   * `blocked` status and no recorded reason at all. The job's `rows[]` has both,
   * and `document_version_id` is the join.
   */
  const qaNotesByVersion = useMemo(() => {
    const out = new Map<string, string[]>();
    for (const row of (watch.job?.progress?.rows ?? []) as any[]) {
      const versionId = row?.document_version_id;
      const notes: string[] = row?.qa_notes ?? [];
      if (versionId && notes.length) out.set(String(versionId), notes);
    }
    return out;
  }, [watch.job]);

  const shown = useMemo(
    () => (filter === "all"
      ? project.generated
      : project.generated.filter((g: any) => g.workflowStatus === filter)),
    [project.generated, filter],
  );

  // Only approved documents may be downloaded, and the server enforces it -- so
  // "download everything" means "download everything that may leave".
  const downloadable = project.generated.filter((g: any) => g.downloadable);

  const sel = useSelection(
    shown,
    (g: any) => String(g.id),
    // An approved document is refused by the server. Saying so before anything
    // is ticked beats confirming a destructive dialog and then being told no.
    (g: any) => g.workflowStatus !== "approved",
    // `MAX_BULK_DOCUMENTS` on the server. A batch produces one document per
    // source row, so a project past this is the ordinary case here.
    100,
  );

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
      {/* The run itself, beside its output. This is the only place the batch's
          error, its per-row failures and its archive can be read -- rows that
          fail outright never become documents, so without this they are
          nowhere. */}
      <BatchProgressPanel watch={watch} className="mb-4" />

      {count === 0 ? (
        <EmptyState
          illustration={<NetworkNodes />}
          title={watch.job ? "No documents yet." : "No generated documents yet."}
          subtitle={watch.job
            ? "They appear here as the batch produces them."
            : "Complete a mapping to generate documents."}
        />
      ) : (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-1.5">
            {WORKFLOW_FILTERS.map((f) => {
              const n = f.key === "all" ? count : counts[f.key] ?? 0;
              return (
                <button
                  key={f.key}
                  onClick={() => { setFilter(f.key); sel.clear(); }}
                  className={cn(
                    "rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
                    filter === f.key
                      ? "border-brand bg-brand/10 text-foreground"
                      : "border-border text-muted-foreground hover:bg-accent",
                    // Shown even at zero, so "there are no blocked documents" is
                    // a thing the screen says rather than a tab that is missing.
                    n === 0 && filter !== f.key && "opacity-50",
                  )}
                >
                  {f.label} <span className="tabular-nums">{n}</span>
                </button>
              );
            })}
          </div>

          <BulkSelectBar
            selection={sel}
            noun="document" pluralNoun="documents"
            names={sel.actionable.filter((g: any) => sel.has(g.id)).map((g: any) => g.filename)}
            blockedNote={sel.blockedCount > 0
              ? `${sel.blockedCount} approved document${sel.blockedCount === 1 ? " is" : "s are"} not selectable and will be left alone.`
              : undefined}
            onDelete={(ids) => api.deleteDocuments(ids)}
            onDone={() => loadProjectDetail(project.id)}
            idle={<p className="text-xs text-muted-foreground">
              Open one to edit its wording; the template's layout is preserved either way.
            </p>}
            extra={<DownloadAllButton documents={downloadable} total={count} />}
          />

          {shown.length === 0 ? (
            /* An empty filter is an empty state, not a failure -- there are
               documents here, none of them are in the state being asked about --
               so this is `PolishedEmpty` and never `ErrorBanner`. */
            <PolishedEmpty
              icon={<FileText className="h-6 w-6" />}
              title={`No documents are ${WORKFLOW_FILTERS.find((f) => f.key === filter)?.label.toLowerCase()}`}
              subtitle="The rest of this project's documents are in one of the other states. Choose All to see every one of them."
            />
          ) : shown.map((g: any) => (
            <div key={g.id} className={cn(
              "flex flex-wrap items-center gap-3 rounded-lg border bg-background/40 p-3",
              sel.has(g.id) ? "border-brand/60 bg-brand/5" : "border-border",
            )}>
              <SelectBox
                id={g.id} selection={sel} label={g.filename}
                blockedReason={g.workflowStatus === "approved"
                  ? "approved, so it cannot be deleted until the approval is withdrawn"
                  : undefined}
              />
              <FileText className="h-5 w-5 text-brand shrink-0" />
              <div className="flex-1 min-w-[12rem]">
                <div className="flex items-center gap-2 flex-wrap">
                  <div className="font-medium text-sm truncate">{g.filename}</div>
                  {/* Two chips, because there are two axes and collapsing them
                      loses one. This one is the engine's and the reviewers'
                      verdict; the select beside it is the person's own lane. */}
                  <StatusChip status={g.status} reason={g.statusReason} />
                </div>
                <div className="text-xs text-muted-foreground">
                  {g.generatedAt} · {g.size} · by {g.generatedBy}
                </div>
              </div>

              <WorkflowSelect
                document={g}
                onDone={() => loadProjectDetail(project.id)}
              />

              {/* Named rather than left as icons. The affordance on a row of a
                  list is "this one is wrong", and making somebody navigate into
                  the document to say so is how objections stop being raised. */}
              {g.currentVersionId && g.workflowStatus !== "approved" && (
                <RequestChangesButton
                  versionId={g.currentVersionId}
                  filename={g.filename}
                  hasOpenReview={Boolean(g.openReviewId)}
                  onDone={() => loadProjectDetail(project.id)}
                />
              )}
              <DownloadDocButton
                documentId={g.id}
                filename={g.filename}
                allowed={g.downloadable}
                status={g.status}
                // The batch's own notes where this screen is still holding the
                // job, and the document's recorded reason where it is not.
                // Nothing beyond those two: if neither exists there is no reason
                // to give, and the control says that rather than inventing one.
                reasons={
                  (g.currentVersionId ? qaNotesByVersion.get(g.currentVersionId) : undefined)
                  ?? (g.statusReason ? [g.statusReason] : [])
                }
              />
              <Link
                to="/projects/$id/edit/$docId"
                params={{ id: project.id, docId: g.id }}
                className="h-8 px-2.5 rounded-lg border border-border text-xs inline-flex items-center gap-1.5 hover:bg-accent"
                title="Edit this document's wording"
              >
                <Pencil className="h-3.5 w-3.5" /> Edit
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
                  g.workflowStatus === "approved"
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

/**
 * Where this document is, in the words of the person working on it.
 *
 * Three options, not five. `approved` is a signature -- the approve action
 * records who and when -- and `blocked` is what the QA gate found when the
 * document was generated; the server refuses both as labels, so offering them
 * here would be offering two choices that answer 409. When one of them is what
 * the document currently *is*, the control says so and explains rather than
 * showing a dropdown whose value cannot be changed to anything sensible.
 */
function WorkflowSelect({ document, onDone }: { document: any; onDone: () => void | Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const locked = document.workflowStatus === "approved" || document.workflowStatus === "blocked";

  if (locked) {
    return (
      <span
        title={document.workflowStatus === "approved"
          ? "Somebody has signed this off. Withdraw the approval to move it again."
          : "This failed its QA checks when it was generated. Fix the template or the source row and generate again — or cancel it."}
        className={cn(
          "inline-flex h-8 items-center rounded-lg border px-2.5 text-xs font-medium",
          document.workflowStatus === "approved"
            ? "border-ai-confident/30 bg-ai-confident/10 text-ai-confident"
            : "border-ai-blocked/30 bg-ai-blocked/10 text-ai-blocked",
        )}
      >
        {WORKFLOW_LABELS[document.workflowStatus as WorkflowStatus]}
      </span>
    );
  }

  return (
    <select
      value={document.workflowStatusSet}
      disabled={busy}
      onChange={async (e) => {
        const next = e.target.value as SettableWorkflowStatus;
        setBusy(true);
        try {
          await api.setDocumentWorkflow(document.id, next);
          await onDone();
        } catch (err: any) {
          toast.error("Could not move this document", { description: err?.message ?? String(err) });
        } finally {
          setBusy(false);
        }
      }}
      className="h-8 rounded-lg border border-border bg-background px-2 text-xs disabled:opacity-50"
      aria-label={`Workflow status for ${document.filename}`}
    >
      {SETTABLE_WORKFLOW.map((s) => (
        <option key={s} value={s}>{WORKFLOW_LABELS[s]}</option>
      ))}
    </select>
  );
}

function RequestChangesButton({ versionId, filename, hasOpenReview, onDone }: {
  versionId: string; filename: string; hasOpenReview: boolean; onDone: () => void | Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");

  if (hasOpenReview) {
    return (
      <Link
        to="/review"
        className="p-1.5 rounded hover:bg-accent text-purple"
        title="Somebody has asked for changes to this document. Open the review queue."
      >
        <MessageSquare className="h-4 w-4" />
      </Link>
    );
  }

  return (
    <>
      <button
        onClick={() => { setReason(""); setOpen(true); }}
        className="p-1.5 rounded hover:bg-accent text-muted-foreground"
        title="Request changes"
      >
        <MessageSquare className="h-4 w-4" />
      </button>
      <ReasonDialog
        open={open} onOpenChange={setOpen}
        title={`What is wrong with ${filename}?`}
        description="This goes to whoever fixes the document, and it stops the letter being signed in the meantime."
        placeholder="e.g. the base salary is from the 2025 band, it should be the 2026 one"
        confirmLabel="Request changes"
        value={reason} onValueChange={setReason}
        onConfirm={() => {
          api.requestChanges(versionId, { reason })
            .then(async () => { setOpen(false); toast.success("Changes requested"); await onDone(); })
            .catch((e: any) => toast.error("Could not request changes", { description: e?.message ?? String(e) }));
        }}
      />
    </>
  );
}


/** Open the template itself for editing, from the project that owns it.
 *
 *  `:from-template` is three-tier, cheapest first: a blueprint already open on
 *  this template is returned as-is; a manifest that already read it is *placed*
 *  deterministically with no model call; and only a template nothing has read
 *  goes to the compiler. Since reading now happens at upload, that third tier is
 *  off the ordinary path -- which is what makes this button instant, and is the
 *  actual fix for "clicking Edit starts a compile".
 *
 *  The one case left is a template whose reading failed or never happened. That
 *  would fall through to tier three and spend minutes in a model behind a button
 *  labelled "Edit wording", so it is disabled and says why rather than doing it
 *  quietly. */
function EditTemplateButton({ templateId, projectId, blueprintId, readable }: {
  templateId: string;
  projectId: string;
  blueprintId?: string;
  /** Whether anything has successfully read this template yet. */
  readable: boolean;
}) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);

  const open = async () => {
    setBusy(true);
    try {
      const bp = blueprintId
        ? { id: blueprintId }
        : await api.blueprintFromTemplate({ template_file_id: templateId });
      navigate({
        to: "/templates/$blueprintId",
        params: { blueprintId: bp.id },
        // Carried so the editor can find its way home, and so publishing returns
        // to this project rather than to the flat template list.
        search: { project: projectId, template: templateId },
      });
    } catch (e: any) {
      toast.error("Could not open this template for editing", {
        description: e?.message ?? String(e),
      });
    } finally {
      setBusy(false);
    }
  };

  const blocked = !readable && !blueprintId;
  return (
    <button
      onClick={open}
      disabled={busy || blocked}
      title={blocked
        ? "This template has not been read yet, so there is nothing to edit. Read it first."
        : "Change the words of the template itself \u2014 the letterhead and layout are kept"}
      className={cn(
        "h-8 px-3 rounded-lg text-xs inline-flex items-center gap-1.5 border border-border",
        blocked ? "opacity-40 cursor-not-allowed" : "hover:bg-accent",
        busy && "opacity-50",
      )}
    >
      {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Pencil className="h-3.5 w-3.5" />}
      Edit wording
    </button>
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
          {/* `whitespace-pre-line` so a description can list what it is about to
              destroy on its own lines. Existing callers pass a single sentence
              and are unaffected. */}
          <AlertDialogDescription className="whitespace-pre-line">
            {description}
          </AlertDialogDescription>
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
  // The response body, kept so the reveal can close its own loop.
  //
  // The stage list and the compile's answer are two separate arrivals: the
  // stages come off a poll against the progress token, the counts come off the
  // request that started it. Without this the panel could narrate the work and
  // then had nothing to say about what was found -- it was the caller holding
  // the answer and dropping it on the floor.
  const [compiled, setCompiled] = useState<any>(null);
  const { newToken, stages, failed } = useCompileProgress(busy);
  const run = async () => {
    const progressToken = newToken();
    setCompiled(null);
    setBusy(true);
    // No toast up front and no time estimate: an uncoloured template goes to a
    // model and can take a couple of minutes, while a colour-coded one is read
    // by the rules almost instantly. Promising a duration we cannot predict is
    // worse than a spinner that plainly means "working".
    try {
      const manifest = await api.compileManifest(templateId, { progressToken });
      setCompiled(manifest);
      await loadProjectDetail(projectId);
      toast.success("Template read", {
        description: "Download the data template to get a spreadsheet with the right columns.",
      });
    } catch (e: any) {
      toast.error("Could not read this template", { description: plainly(String(e?.message ?? e)) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col items-end gap-2">
      <button
        onClick={run}
        disabled={busy}
        title="Read this template again and work out what data it needs"
        className="h-8 px-3 rounded-lg bg-gradient-brand text-white text-xs inline-flex items-center gap-1.5 hover:opacity-90 disabled:opacity-60"
      >
        {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <WandIcon className="h-3.5 w-3.5" />}
        {busy ? "Reading…" : "Read again"}
      </button>
      {/* Only while it runs. A finished compile is described by the row itself
          -- "Compiled · 25 fields, 5 conditions" -- and leaving the stage list
          behind would say the same thing twice. */}
      {busy && (
        <div className="w-full min-w-[22rem]">
          <CompileProgressList stages={stages} failed={failed} result={compiled} />
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
  description,
  accept,
  onUpload,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  description?: string;
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
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description ?? "Choose a file to upload."}</DialogDescription>
        </DialogHeader>
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
          <stop offset="0" stopColor="var(--color-brand)" /><stop offset="1" stopColor="var(--color-purple)" />
        </linearGradient>
      </defs>
      <rect x="15" y="70" width="130" height="10" fill="var(--color-accent)" opacity="0.6" />
      <rect x="20" y="82" width="120" height="8" fill="var(--color-accent)" opacity="0.5" />
      <rect x="25" y="93" width="110" height="6" fill="var(--color-accent)" opacity="0.4" />
      <rect x="45" y="20" width="60" height="50" rx="4" fill="url(#tb)" opacity="0.9" />
      <path d="M110 30 L130 20 L130 55 L110 65 Z" fill="var(--color-purple)" opacity="0.8" />
      <path d="M75 5 L75 20 M65 12 L75 20 L85 12" stroke="url(#tb)" strokeWidth="2" fill="none" />
    </svg>
  );
}
function SourceBox() {
  return (
    <svg viewBox="0 0 160 130" className="w-40 h-32">
      <defs>
        <linearGradient id="sb" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="var(--color-purple)" /><stop offset="1" stopColor="var(--color-brand)" />
        </linearGradient>
      </defs>
      <path d="M45 40 L80 25 L115 40 L80 55 Z" fill="url(#sb)" opacity="0.8" />
      <path d="M45 40 L45 85 L80 100 L80 55 Z" fill="var(--color-muted)" />
      <path d="M115 40 L115 85 L80 100 L80 55 Z" fill="var(--color-secondary)" />
      <path d="M25 70 L45 65 M25 80 L45 75" stroke="var(--color-brand)" strokeWidth="2" markerEnd="url(#arr)" />
    </svg>
  );
}
function NetworkNodes() {
  return (
    <svg viewBox="0 0 160 130" className="w-40 h-32">
      <defs>
        <linearGradient id="nn" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="var(--color-brand)" /><stop offset="1" stopColor="var(--color-purple)" />
        </linearGradient>
      </defs>
      <path d="M80 30 L40 80 M80 30 L80 80 M80 30 L120 80" stroke="url(#nn)" strokeWidth="2" />
      <rect x="70" y="20" width="20" height="20" fill="url(#nn)" />
      <rect x="30" y="70" width="20" height="20" fill="var(--color-secondary)" />
      <rect x="70" y="70" width="20" height="20" fill="var(--color-secondary)" />
      <rect x="110" y="70" width="20" height="20" fill="var(--color-secondary)" />
    </svg>
  );
}
