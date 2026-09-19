import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { useStore, FUNCTION_COLORS } from "@/lib/store";
import { api } from "@/lib/api";
import type { FunctionKey, ProjectStatus } from "@/lib/types";
import { StatusBadge } from "@/components/status-badge";
import { CreateProjectSheet } from "@/components/create-project-sheet";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Search,
  Filter,
  RefreshCcw,
  Plus,
  Pencil,
  Trash2,
  MoreHorizontal,
  FileText,
  ChevronLeft,
  ChevronRight,
  Archive,
  ExternalLink,
  Loader2,
  Sparkles,
  FolderKanban,
  CheckCircle2,
  AlertTriangle,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { FadeIn, Stagger, StaggerItem, useCountUp } from "@/components/motion";
import { SkeletonBar, TableSkeleton, PolishedEmpty } from "@/components/skeletons";
import { ErrorBanner } from "@/components/error-banner";
import { BulkSelectBar, SelectBox, useSelection } from "@/components/bulk-select";
import { Checkbox } from "@/components/ui/checkbox";
// JPEG, not PNG. The source artwork is a photographic render with no
// transparency, and PNG stores that losslessly for 1.1 MB -- five times the
// weight of the whole rest of this route, on the first screen after sign-in.
import aiDocumentHero from "@/assets/ai-document-hero.jpg";

export const Route = createFileRoute("/_app/dashboard")({
  head: () => ({
    meta: [
      { title: "Dashboard — DocuMind AI" },
      { name: "description", content: "Turn a Word template and a spreadsheet into finished documents." },
      { property: "og:title", content: "Dashboard — DocuMind AI" },
      { property: "og:description", content: "One workspace for every document your templates produce." },
    ],
  }),
  component: Dashboard,
});

const PAGE_SIZE = 10;

/** The raw `status` values the backend stores and filters on -- the keys of
 * STATUS_MAP in src/lib/store.ts. `archived` is a real stored status even though
 * STATUS_MAP folds it onto the "Completed" badge, so it is filterable here. */
const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: "pending", label: "Pending" },
  { value: "in_progress", label: "In Progress" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "archived", label: "Archived" },
];

const DISPLAY_STATUS: Record<string, ProjectStatus> = {
  pending: "Pending",
  in_progress: "In Progress",
  completed: "Completed",
  failed: "Failed",
  archived: "Completed",
};

interface Row {
  id: string;
  displayId: string;
  name: string;
  description: string;
  documentType: string;
  function: FunctionKey;
  createdAt: string;
  modifiedAt: string;
  status: ProjectStatus;
  /** The stored value, not the badge label: Archive has nothing to do once the
   * project is already archived, and the badge cannot tell us that. */
  rawStatus: string;
}

function toDisplayDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-US", {
    month: "short", day: "numeric", year: "numeric",
    hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true,
  });
}

// The store's loadProjects() takes only `q`, and this screen also filters by
// status and pages, so it queries the API directly and shapes the rows it needs.
// If loadProjects ever grows those options this mapping should go back to it.
function mapRow(p: any): Row {
  return {
    id: p.id,
    displayId: String(p.display_id),
    name: p.name,
    description: p.description ?? "",
    documentType: p.document_type,
    function: p.function as FunctionKey,
    createdAt: toDisplayDateTime(p.created_at),
    modifiedAt: toDisplayDateTime(p.updated_at),
    status: DISPLAY_STATUS[p.status] ?? "Pending",
    rawStatus: p.status,
  };
}

function Dashboard() {
  const currentUser = useStore((s) => s.currentUser);
  const navigate = useNavigate();

  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<string>("all");
  const [offset, setOffset] = useState(0);
  const [createOpen, setCreateOpen] = useState(false);

  const [rows, setRows] = useState<Row[]>([]);
  /** Size of the current result set when it can be established; null when the
   * server's count cannot be trusted for this query (see load()). */
  const [total, setTotal] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(true);
  /** The server's own words for why the last list request failed, kept in state
   *  rather than thrown at a toast: a toast is gone in four seconds, and the
   *  failure that left this table empty needs to stay next to the empty table
   *  for as long as it is empty. Cleared only by a load that succeeds. */
  const [loadError, setLoadError] = useState<string | null>(null);

  const [renameTarget, setRenameTarget] = useState<Row | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Row | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  // Keystrokes and page clicks can land out of order; only the newest response
  // is allowed to write state, or a slow earlier query overwrites a later one.
  const reqRef = useRef(0);
  const load = useCallback(async () => {
    const seq = ++reqRef.current;
    setLoading(true);
    try {
      const res = await api.listProjects(search || undefined, {
        status: status === "all" ? undefined : status,
        limit: PAGE_SIZE,
        offset,
      });
      if (seq !== reqRef.current) return;

      const page = res.items ?? [];

      // A page past the end (a delete emptied it, or the filter narrowed) strands
      // the user on an empty table; step back instead. offset only shrinks, so
      // this terminates.
      if (page.length === 0 && offset > 0) {
        setOffset((o) => Math.max(0, o - PAGE_SIZE));
        return;
      }

      // `total` counts the rows matching the current q/status, which is what a
      // pager has to divide. It used to count every project in the organisation,
      // so a filtered list offered pages that were always empty.
      const known = res.total;

      setRows(page.map(mapRow));
      setTotal(known);
      setHasNext(offset + page.length < known);
      setLoadError(null);
    } catch (e: any) {
      if (seq !== reqRef.current) return;
      setLoadError(e?.message ?? String(e));
    } finally {
      if (seq === reqRef.current) setLoading(false);
    }
  }, [search, status, offset]);

  // Debounced so each keystroke doesn't fire a request, and filtered by the
  // server rather than over whatever happens to be loaded on this page.
  useEffect(() => {
    const t = setTimeout(() => { void load(); }, search ? 250 : 0);
    return () => clearTimeout(t);
  }, [load, search]);

  const resetToFirstPage = () => setOffset(0);

  // Selection is by project id, which matters more here than on the other lists:
  // this table is paged, and `rows` is replaced wholesale on every search, filter
  // and page change. `useSelection` intersects the ticked ids with what is
  // currently on screen, so a project ticked on page 1 is not silently deleted
  // from page 2 -- the count and the request only ever cover visible rows.
  const sel = useSelection(rows, (r: Row) => String(r.id));

  const runRowAction = async (row: Row, action: () => Promise<unknown>, success: string, failure: string): Promise<boolean> => {
    // Returning undefined here read as failure to `confirmDelete`, which then
    // left the dialog open with no toast and no explanation of why nothing
    // happened. Unreachable while the trigger is disabled mid-action, but the
    // caller is entitled to a definite answer.
    if (busyId) return false;
    setBusyId(row.id);
    try {
      await action();
      toast.success(success, { description: row.name });
      // The row has to leave (or change) on screen, or the action reads as a no-op.
      await load();
      return true;
    } catch (e: any) {
      toast.error(failure, { description: e?.message ?? String(e) });
      return false;
    } finally {
      setBusyId(null);
    }
  };

  const archive = (row: Row) =>
    runRowAction(row, () => api.archiveProject(row.id), "Project archived", "Could not archive project");

  const confirmDelete = async () => {
    const row = deleteTarget;
    if (!row) return;
    // Emptying the last page is handled by the reload inside runRowAction, which
    // walks the offset back -- doing it here too would skip a page.
    const ok = await runRowAction(row, () => api.deleteProject(row.id), "Project deleted", "Could not delete project");
    if (ok) setDeleteTarget(null);
  };

  const firstName = currentUser.split(" ")[0];
  const shownCount = total;
  const activeStatus = STATUS_FILTERS.find((s) => s.value === status);
  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const narrowed = search !== "" || status !== "all";

  // Only when there is nothing on screen yet. A refresh or a page step keeps the
  // rows it already has and lets them be replaced in place -- swapping a full
  // table for shimmer on every keystroke of the debounced search is more motion
  // than the wait deserves.
  const showSkeleton = loading && rows.length === 0;

  // What we are allowed to print as a number.
  //
  // A request that failed tells us nothing about the workspace, so nothing
  // derived from it may be rendered as a figure: a tile reading "0" says "we
  // counted, and there were none" when the truth is "we asked, and never found
  // out". Three states rather than two --
  //
  //   showSkeleton    the first request is still in flight and nothing has ever
  //                   landed: the bars stand in, as before.
  //   figuresUnknown  it failed and no earlier page survived. Every derived
  //                   figure becomes a dash with the reason beside it.
  //   figuresStale    it failed but an earlier page is still on screen, so the
  //                   figures are real -- just not refreshed, and labelled so.
  const figuresUnknown = loadError != null && rows.length === 0 && !showSkeleton;
  const figuresStale = loadError != null && rows.length > 0;
  /** Marks a caption whose figure is the last one that loaded, not the current one. */
  const asOf = (caption: string) => (figuresStale ? `${caption} · not refreshed` : caption);

  // Counted off the rows already on screen rather than fetched: this route asks
  // the server for exactly one page, and a second count query would fire again
  // on every keystroke and every page step. The captions say "on this page" so
  // the figures cannot be read as a claim about the whole workspace.
  const onPage = {
    running: rows.filter((r) => r.rawStatus === "in_progress").length,
    completed: rows.filter((r) => r.rawStatus === "completed" || r.rawStatus === "archived").length,
    failed: rows.filter((r) => r.rawStatus === "failed").length,
  };

  // Under `figuresUnknown` the tiles keep their labels and their places -- the
  // strip is the shape of the page and removing it would move everything below
  // it -- but every value is null and every tone drops to idle: a "Needs a look"
  // tile still glowing blocked, or a spinner still turning, would be claiming a
  // state we no longer have any evidence for.
  const unknownCaption = "couldn't be loaded";
  const stats: StatTileProps[] = figuresUnknown
    ? [
        { label: "Projects", value: null, caption: unknownCaption, tone: "idle", icon: FolderKanban },
        { label: "Running now", value: null, caption: unknownCaption, tone: "idle", icon: Loader2 },
        { label: "Completed", value: null, caption: unknownCaption, tone: "idle", icon: CheckCircle2 },
        { label: "Needs a look", value: null, caption: unknownCaption, tone: "idle", icon: AlertTriangle },
      ]
    : [
        {
          label: "Projects",
          value: shownCount,
          caption: asOf(narrowed ? "matching this view" : "in your workspace"),
          tone: "brand",
          icon: FolderKanban,
        },
        {
          label: "Running now",
          value: onPage.running,
          caption: asOf("on this page"),
          tone: onPage.running > 0 ? "active" : "idle",
          icon: Loader2,
          // Not while the figure is stale: a spinner is a claim that something is
          // happening right now, and a failed refresh is the one moment we cannot
          // make it.
          spin: !figuresStale && onPage.running > 0,
        },
        {
          label: "Completed",
          value: onPage.completed,
          caption: asOf("on this page"),
          tone: onPage.completed > 0 ? "confident" : "idle",
          icon: CheckCircle2,
        },
        {
          label: "Needs a look",
          value: onPage.failed,
          caption: asOf("on this page"),
          tone: onPage.failed > 0 ? "blocked" : "idle",
          icon: AlertTriangle,
        },
      ];

  const clearFilters = () => {
    setSearch("");
    setStatus("all");
    setOffset(0);
  };

  return (
    <div className="p-8 space-y-8">
      {/* The hero.
       *
       *  The artwork is a real <img> rather than a CSS background: it is the one
       *  thing on this page that explains what the product does at a glance, and
       *  a background image is nothing at all to a screen reader.
       *
       *  The scrim over it is mixed from `surface`, not from black. This app ships
       *  a light theme as well, and a black wash that makes copy legible on the
       *  dark ground turns the light one into white text on charcoal inside an
       *  otherwise white page. Reading the same token as the panel it sits in, the
       *  artwork fades toward whatever the page ground happens to be and the copy
       *  keeps a near-solid field behind it in both. */}
      <FadeIn>
        <section className="relative isolate flex min-h-[300px] items-center overflow-hidden rounded-2xl surface-raised md:min-h-[360px]">
          <img
            src={aiDocumentHero}
            alt="A template becoming a stack of finished documents"
            className="pointer-events-none absolute inset-0 h-full w-full max-w-full object-cover object-[68%_50%]"
          />
          {/* Two scrims. The horizontal one carries the copy on a wide screen,
              where the text occupies the left half and the artwork can keep the
              right; on a narrow one the copy runs the full width, so the same
              gradient holds far more of the surface and the picture becomes a
              tint behind the words rather than something competing with them. */}
          <div className="pointer-events-none absolute inset-0 bg-gradient-to-r from-surface from-30% via-surface/95 to-surface/65 md:via-surface/85 md:to-surface/20" />
          <div className="pointer-events-none absolute inset-0 bg-gradient-to-t from-surface via-surface/45 to-transparent md:via-transparent" />
          <div className="pointer-events-none absolute inset-0 grid-noise opacity-40" />

          <div className="relative max-w-2xl p-8 md:p-12">
            <span className="inline-flex items-center gap-2 rounded-full border border-brand/30 bg-brand/10 px-3 py-1 text-xs font-medium text-brand">
              <Sparkles className="h-3.5 w-3.5" />
              Document workspace
            </span>
            <h1 className="mt-4 text-4xl md:text-5xl font-bold tracking-tight text-gradient">
              Welcome, {firstName}
            </h1>
            <p className="mt-3 max-w-xl text-[15px] leading-relaxed text-muted-foreground">
              Turn a Word template and a spreadsheet into finished documents, one for every row.
              You check how your data fits the template before anything is generated.
            </p>
            <div className="mt-7">
              <button
                onClick={() => setCreateOpen(true)}
                className="sheen h-10 inline-flex items-center gap-2 rounded-lg bg-gradient-brand px-5 text-sm font-medium text-white hover:opacity-90"
              >
                <span className="sheen-layer" />
                <Plus className="relative h-4 w-4" />
                <span className="relative">Create project</span>
              </button>
            </div>
          </div>
        </section>
      </FadeIn>

      <Stagger className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {stats.map((s) => (
          <StaggerItem key={s.label}>
            <StatTile {...s} loading={showSkeleton} />
          </StaggerItem>
        ))}
      </Stagger>

      {/* Content studio */}
      <div>
        <div className="flex items-end justify-between gap-4 flex-wrap">
          <div>
            {/* The count is held back until the first page lands: a heading that
                says "(0)" and then corrects itself to "(41)" has told the user
                something untrue for as long as the request took. It is dropped
                entirely -- not shown as "(0)" -- when the load failed and there
                is nothing to count from, since the count is unknown rather than
                nought; the banner below carries why. */}
            <h2 className="text-2xl font-semibold tracking-tight">
              Content studio{" "}
              {showSkeleton ? (
                <SkeletonBar className="inline-block h-4 w-10 align-middle" />
              ) : figuresUnknown ? null : (
                <span className="text-muted-foreground font-normal">({shownCount})</span>
              )}
            </h2>
            <p className="text-sm text-muted-foreground mt-1">
              Start by creating a project. Everything you create will appear here for easy access and management.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <input
                value={search}
                onChange={(e) => { setSearch(e.target.value); resetToFirstPage(); }}
                className="h-9 rounded-lg bg-surface border border-border pl-9 pr-3 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring/50 w-56"
                placeholder="Search projects…"
              />
            </div>

            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  title={activeStatus ? `Status: ${activeStatus.label}` : "Filter by status"}
                  className={cn(
                    "h-9 rounded-lg surface-raised hover:bg-accent flex items-center justify-center gap-1.5 text-muted-foreground",
                    activeStatus ? "px-3 border-brand/40 text-brand" : "w-9",
                  )}
                >
                  <Filter className="h-4 w-4" />
                  {activeStatus && <span className="text-xs font-medium">{activeStatus.label}</span>}
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-44">
                <DropdownMenuLabel>Filter by status</DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuRadioGroup
                  value={status}
                  onValueChange={(v) => { setStatus(v); resetToFirstPage(); }}
                >
                  <DropdownMenuRadioItem value="all">All statuses</DropdownMenuRadioItem>
                  {STATUS_FILTERS.map((s) => (
                    <DropdownMenuRadioItem key={s.value} value={s.value}>
                      {s.label}
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>

            <button
              onClick={() => void load()}
              disabled={loading}
              title="Refresh"
              className="h-9 w-9 rounded-lg surface-raised hover:bg-accent flex items-center justify-center text-muted-foreground disabled:opacity-50"
            >
              <RefreshCcw className={cn("h-4 w-4", loading && "animate-spin")} />
            </button>
            <button
              onClick={() => setCreateOpen(true)}
              className="h-9 inline-flex items-center gap-2 rounded-lg bg-gradient-brand text-white px-4 text-sm font-medium hover:opacity-90"
            >
              <Plus className="h-4 w-4" /> Create project
            </button>
          </div>
        </div>

        {/* Above the table rather than over it: the rows underneath may still be
            the last good page, and hiding them behind a failure would throw away
            the only data the user still has. */}
        {loadError && (
          <ErrorBanner
            className="mt-6"
            title={figuresStale ? "Could not refresh your projects" : "Could not load your projects"}
            message={
              figuresStale
                ? "Nothing has been changed — the refresh failed, so the rows and figures below are the ones from the last load that succeeded and may be out of date."
                : "Nothing has been changed — the list just could not be fetched, so the counts above are unknown rather than zero. Try again, or check that you are still signed in."
            }
            detail={loadError}
            onRetry={() => { void load(); }}
            retrying={loading}
          />
        )}

        {/* Only once something is ticked. A destructive control sitting on the
            dashboard permanently, with nothing selected, is one mis-click from a
            dialog nobody meant to open. */}
        {sel.chosen.length > 0 && (
          <div className="mt-6 rounded-xl border border-destructive/30 bg-destructive/5 px-4 py-2">
            <BulkSelectBar
              selection={sel}
              noun="project" pluralNoun="projects"
              names={rows.filter((r) => sel.has(r.id)).map((r) => r.name)}
              onDelete={(ids) => api.deleteProjects(ids)}
              onDone={() => load()}
            />
          </div>
        )}

        {/* Table */}
        <div className="mt-6 rounded-xl surface-raised overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="bg-gradient-brand text-white text-left text-[11px] uppercase tracking-wider">
                  <th className="px-4 py-3 w-10">
                    <Checkbox
                      checked={sel.allChosen}
                      onCheckedChange={(v) => sel.setAll(v === true)}
                      aria-label="Select all projects on this page"
                      className="border-white/70 data-[state=checked]:bg-white data-[state=checked]:text-brand"
                    />
                  </th>
                  <th className="px-4 py-3 font-semibold whitespace-nowrap">Project name</th>
                  <th className="px-4 py-3 font-semibold whitespace-nowrap">Project ID</th>
                  <th className="px-4 py-3 font-semibold whitespace-nowrap">Document type</th>
                  <th className="px-4 py-3 font-semibold whitespace-nowrap">Function</th>
                  <th className="px-4 py-3 font-semibold whitespace-nowrap">Created on</th>
                  <th className="px-4 py-3 font-semibold whitespace-nowrap">Modified on</th>
                  <th className="px-4 py-3 font-semibold whitespace-nowrap">Status</th>
                  <th className="px-4 py-3 font-semibold whitespace-nowrap text-right pr-6">Actions</th>
                </tr>
              </thead>
              {/* The skeleton stands in only for the very first page, and it is
                  nine columns wide on purpose: a placeholder narrower than the
                  table it replaces makes the header jump sideways the moment the
                  rows land. */}
              {showSkeleton ? (
                <TableSkeleton rows={6} cols={9} />
              ) : (
                <tbody>
                  {rows.map((p, idx) => (
                    <tr
                      key={p.id}
                      className={cn(
                        "border-t border-border hover:bg-accent/40 transition-colors group",
                        idx % 2 === 1 && "bg-surface-elevated/30",
                        sel.has(p.id) && "bg-brand/5",
                        busyId === p.id && "opacity-60",
                      )}
                    >
                      <td className="px-4 py-3">
                        <SelectBox id={p.id} selection={sel} label={p.name} />
                      </td>
                      <td className="px-4 py-3 max-w-[220px]">
                        <Link
                          to="/projects/$id"
                          params={{ id: p.id }}
                          className="block truncate font-medium hover:text-brand hover:underline"
                          title={p.name}
                        >
                          {p.name}
                        </Link>
                      </td>
                      <td className="px-4 py-3 font-mono text-sm text-muted-foreground whitespace-nowrap">{p.displayId}</td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        <span className="inline-flex items-center gap-2">
                          <FileText className="h-4 w-4 text-muted-foreground shrink-0" />
                          <span className="truncate">{p.documentType}</span>
                        </span>
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        <span
                          className={cn(
                            "inline-flex items-center whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium leading-5",
                            FUNCTION_COLORS[p.function],
                          )}
                        >
                          {p.function}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm whitespace-nowrap">
                        <DateCell v={p.createdAt} />
                      </td>
                      <td className="px-4 py-3 text-sm whitespace-nowrap">
                        <DateCell v={p.modifiedAt} />
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        <StatusBadge status={p.status} />
                      </td>
                      <td className="px-4 py-3 pr-6">
                        {/* Rename stays on the row because it is the everyday edit;
                            Delete lives in the menu instead of beside it, so the one
                            irreversible action is not a mis-click away from the one
                            harmless one. */}
                        <div className="flex items-center justify-end gap-1 opacity-70 group-hover:opacity-100">
                          <button
                            onClick={() => setRenameTarget(p)}
                            disabled={busyId != null}
                            title="Rename project"
                            className="p-1.5 rounded hover:bg-accent text-muted-foreground hover:text-foreground disabled:opacity-50"
                          >
                            <Pencil className="h-4 w-4" />
                          </button>
                          <DropdownMenu>
                            <DropdownMenuTrigger asChild>
                              <button
                                disabled={busyId != null}
                                title="More options"
                                className="p-1.5 rounded hover:bg-accent text-muted-foreground disabled:opacity-50"
                              >
                                {busyId === p.id
                                  ? <Loader2 className="h-4 w-4 animate-spin" />
                                  : <MoreHorizontal className="h-4 w-4" />}
                              </button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end" className="w-44">
                              <DropdownMenuItem
                                onSelect={() => navigate({ to: "/projects/$id", params: { id: p.id } })}
                              >
                                <ExternalLink className="h-4 w-4" /> Open
                              </DropdownMenuItem>
                              <DropdownMenuItem onSelect={() => setRenameTarget(p)}>
                                <Pencil className="h-4 w-4" /> Rename
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                disabled={p.rawStatus === "archived"}
                                onSelect={() => { void archive(p); }}
                              >
                                <Archive className="h-4 w-4" />
                                {p.rawStatus === "archived" ? "Archived" : "Archive"}
                              </DropdownMenuItem>
                              <DropdownMenuSeparator />
                              <DropdownMenuItem
                                className="text-destructive focus:text-destructive"
                                onSelect={() => setDeleteTarget(p)}
                              >
                                <Trash2 className="h-4 w-4" /> Delete
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {rows.length === 0 && (
                    <tr>
                      <td colSpan={9} className="p-0">
                        {loadError ? (
                          // The banner above already carries the server's words;
                          // repeating them inside the table would say it twice, and
                          // PolishedEmpty would say the opposite -- that there is
                          // nothing here -- when the truth is that we do not know.
                          <div className="px-4 py-14 text-center text-sm text-muted-foreground">
                            Nothing to show while the list is unavailable.
                          </div>
                        ) : narrowed ? (
                          <PolishedEmpty
                            className="rounded-none border-0 bg-transparent py-16"
                            icon={<Search className="h-6 w-6" />}
                            title="No projects match that"
                            subtitle="Nothing here matches your search or the status filter. Widen either one to see more of the workspace."
                            action={
                              <Button variant="outline" onClick={clearFilters}>
                                Clear search and filters
                              </Button>
                            }
                          />
                        ) : (
                          <PolishedEmpty
                            className="rounded-none border-0 bg-transparent py-16"
                            icon={<FileText className="h-6 w-6" />}
                            title="No projects yet"
                            subtitle="A project pairs one Word template with the spreadsheet that fills it. Create one and every document it produces lands here."
                            action={
                              <Button onClick={() => setCreateOpen(true)}>
                                <Plus className="h-4 w-4" /> Create project
                              </Button>
                            }
                          />
                        )}
                      </td>
                    </tr>
                  )}
                </tbody>
              )}
            </table>
          </div>
          <div className="border-t border-border px-4 py-3 flex items-center justify-between text-xs text-muted-foreground">
            <div>
              {/* "Showing 0 projects" is a measurement, and after a failed
                  request there is nothing to measure -- so the failure says so
                  in its own words instead of borrowing the empty result's. */}
              {showSkeleton ? (
                <SkeletonBar className="h-3 w-36" />
              ) : figuresUnknown ? (
                "Count unavailable — the list could not be loaded"
              ) : rows.length === 0 ? (
                "Showing 0 projects"
              ) : figuresStale ? (
                `Showing ${offset + 1}-${offset + rows.length} of ${total} · not refreshed`
              ) : (
                `Showing ${offset + 1}-${offset + rows.length} of ${total}`
              )}
            </div>
            <div className="flex items-center gap-1">
              <button
                onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
                disabled={offset === 0 || loading}
                title="Previous page"
                className="h-7 w-7 rounded border border-border hover:bg-accent flex items-center justify-center disabled:opacity-40 disabled:hover:bg-transparent"
              >
                <ChevronLeft className="h-3.5 w-3.5" />
              </button>
              <span className="px-2">{page}</span>
              <button
                onClick={() => setOffset((o) => o + PAGE_SIZE)}
                disabled={!hasNext || loading}
                title="Next page"
                className="h-7 w-7 rounded border border-border hover:bg-accent flex items-center justify-center disabled:opacity-40 disabled:hover:bg-transparent"
              >
                <ChevronRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </div>
      </div>

      <CreateProjectSheet open={createOpen} onOpenChange={setCreateOpen} />

      <RenameProjectDialog
        target={renameTarget}
        onClose={() => setRenameTarget(null)}
        onSaved={() => { void load(); }}
      />

      <AlertDialog open={deleteTarget != null} onOpenChange={(o) => { if (!o) setDeleteTarget(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete “{deleteTarget?.name}”?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently removes the project along with its templates, source files and every
              document generated from it. This cannot be undone — archive it instead if you only want
              it out of the way.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busyId != null}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={busyId != null}
              // Radix closes on click; hold the dialog open so a failure stays
              // in front of the user instead of vanishing behind a toast.
              onClick={(e) => { e.preventDefault(); void confirmDelete(); }}
            >
              {busyId != null ? "Deleting…" : "Delete project"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

type StatTone = "brand" | "active" | "confident" | "blocked" | "idle";

/** The intelligence ramp rather than the raw palette. "Running" and "sure" have
 *  to be the same colour on this page as they are in the processing banner and
 *  the confidence bands, or every screen ends up telling its own story about
 *  what amber means. `brand` is here for the one tile that is not a state at all
 *  -- the workspace's own count. */
const STAT_TONES: Record<StatTone, string> = {
  brand: "text-brand border-brand/30 bg-brand/10",
  active: "text-ai-active border-ai-active/30 bg-ai-active/10",
  confident: "text-ai-confident border-ai-confident/30 bg-ai-confident/10",
  blocked: "text-ai-blocked border-ai-blocked/30 bg-ai-blocked/10",
  idle: "text-muted-foreground border-border bg-muted/60",
};

interface StatTileProps {
  label: string;
  /** null when there is no figure to show -- the load failed and no earlier page
   *  survived. A dash, never a zero: the two mean opposite things. */
  value: number | null;
  caption: string;
  tone: StatTone;
  icon: LucideIcon;
  spin?: boolean;
  loading?: boolean;
}

function StatTile({ label, value, caption, tone, icon: Icon, spin = false, loading = false }: StatTileProps) {
  // The count-up is decoration laid over a real figure, never a substitute for
  // one: the target is whatever the API returned, and `useCountUp` hands that
  // number straight back under prefers-reduced-motion. While the first page is
  // still in flight the bar stands in for it, because a confident "0" is a claim
  // about the workspace that we cannot yet make.
  //
  // It is switched off in both of the states that have no figure -- pending and
  // unknown -- so an absent measurement can never be animated through zero on
  // its way to a dash.
  const unknown = value == null;
  const counted = Math.round(useCountUp(value ?? 0, 700, !unknown && !loading));

  return (
    <div className="h-full rounded-xl surface-raised p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">{label}</p>
          {loading ? (
            <SkeletonBar className="mt-2.5 h-7 w-14" />
          ) : (
            <p
              className={cn(
                "mt-1 text-3xl font-semibold tabular-nums tracking-tight",
                unknown && "text-muted-foreground/50",
              )}
            >
              {unknown ? "—" : counted}
            </p>
          )}
          <p className="mt-1 text-xs text-muted-foreground">{caption}</p>
        </div>
        <span
          className={cn(
            "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border",
            STAT_TONES[tone],
          )}
        >
          <Icon className={cn("h-4 w-4", spin && "animate-spin")} />
        </span>
      </div>
    </div>
  );
}

function RenameProjectDialog({
  target,
  onClose,
  onSaved,
}: {
  target: Row | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (target) {
      setName(target.name);
      setDescription(target.description);
    }
  }, [target]);

  const save = async () => {
    if (!target || saving || !name.trim()) return;
    setSaving(true);
    try {
      await api.patchProject(target.id, { name: name.trim(), description });
      toast.success("Project updated", { description: name.trim() });
      onClose();
      onSaved();
    } catch (e: any) {
      toast.error("Could not update project", { description: e?.message ?? String(e) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={target != null} onOpenChange={(o) => { if (!o && !saving) onClose(); }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Edit project</DialogTitle>
          <DialogDescription>Rename this project or update its description.</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="rename-name">Project name *</Label>
            <Input
              id="rename-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void save(); } }}
              autoFocus
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="rename-desc">Description</Label>
            <Textarea
              id="rename-desc"
              rows={3}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Optional details about this project"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>Cancel</Button>
          <Button onClick={() => void save()} disabled={saving || !name.trim()}>
            {saving ? "Saving…" : "Save changes"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DateCell({ v }: { v: string }) {
  const [date, time] = v.includes(",") ? [v.split(",").slice(0, 2).join(","), v.split(",")[2]?.trim() ?? ""] : [v, ""];
  return (
    <div className="leading-tight">
      <div>{date}</div>
      {time && <div className="text-xs text-muted-foreground">{time}</div>}
    </div>
  );
}
