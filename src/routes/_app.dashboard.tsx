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
} from "lucide-react";
import { cn } from "@/lib/utils";
import { FadeIn } from "@/components/motion";
import { BulkSelectBar, SelectBox, useSelection } from "@/components/bulk-select";
import { Checkbox } from "@/components/ui/checkbox";

export const Route = createFileRoute("/_app/dashboard")({
  head: () => ({
    meta: [
      { title: "Dashboard — DocuMind AI" },
      { name: "description", content: "Manage your AI-assisted document generation projects." },
      { property: "og:title", content: "Dashboard — DocuMind AI" },
      { property: "og:description", content: "Content studio for AI-generated documents." },
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
    } catch (e: any) {
      if (seq !== reqRef.current) return;
      toast.error("Could not load projects", { description: e?.message ?? String(e) });
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

  return (
    <div className="p-8 space-y-8">
      {/* Welcome banner */}
      <FadeIn className="relative overflow-hidden rounded-2xl surface-raised p-8 md:p-10">
        <div className="absolute inset-0 bg-hero-orbs opacity-70 pointer-events-none" />
        <div className="relative grid md:grid-cols-[1fr_auto] gap-8 items-center">
          <div>
            <h1 className="text-4xl md:text-5xl font-bold tracking-tight text-gradient">
              Welcome, {firstName}
            </h1>
            <p className="mt-3 text-muted-foreground max-w-xl">
              Create and manage AI-assisted document generation projects from a single workspace.
            </p>
          </div>
          <WelcomeIllustration />
        </div>
      </FadeIn>

      {/* Content studio */}
      <div>
        <div className="flex items-end justify-between gap-4 flex-wrap">
          <div>
            <h2 className="text-2xl font-semibold tracking-tight">
              Content studio <span className="text-muted-foreground font-normal">({shownCount})</span>
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
                    <td colSpan={9} className="px-4 py-12 text-center text-muted-foreground text-sm">
                      {loading
                        ? "Loading projects…"
                        : search || status !== "all"
                          ? "No projects match your search."
                          : "No projects yet. Create one to get started."}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="border-t border-border px-4 py-3 flex items-center justify-between text-xs text-muted-foreground">
            <div>
              {rows.length === 0
                ? "Showing 0 projects"
                : `Showing ${offset + 1}-${offset + rows.length} of ${total}`}
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

/** A template on the left, a filled document on the right.
 *
 *  Every fill and stroke reads a theme token rather than a literal. It used to
 *  hardcode the dark palette's oklch values -- `fill="oklch(0.22 0.02 270)"` and
 *  so on -- which meant it did not follow a token change and was already broken
 *  in light mode: dark grey panels on a white page, with grey-on-grey text lines
 *  that vanished entirely. */
function WelcomeIllustration() {
  return (
    <svg viewBox="0 0 220 160" className="w-56 md:w-64 h-auto" role="img"
         aria-label="A template on the left, filled into a finished document on the right">
      <defs>
        <linearGradient id="g1" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="var(--color-brand)" />
          <stop offset="1" stopColor="var(--color-purple)" />
        </linearGradient>
      </defs>
      <rect x="20" y="30" width="80" height="100" rx="6"
            fill="var(--color-muted)" stroke="var(--color-border-strong)" />
      <rect x="30" y="45" width="60" height="4" rx="2" fill="url(#g1)" />
      {[55, 62, 69].map((y, i) => (
        <rect key={y} x="30" y={y} width={[50, 55, 45][i]} height="3" rx="1.5"
              fill="var(--color-muted-foreground)" opacity="0.55" />
      ))}
      <circle cx="140" cy="60" r="18" fill="url(#g1)" opacity="0.9" />
      <path d="M105 70 L125 65" stroke="url(#g1)" strokeWidth="2" />
      <rect x="130" y="90" width="80" height="50" rx="6"
            fill="var(--color-accent)" stroke="url(#g1)" />
      <rect x="140" y="100" width="60" height="3" rx="1.5" fill="url(#g1)" />
      {[108, 116, 124].map((y, i) => (
        <rect key={y} x="140" y={y} width={[55, 50, 45][i]} height="3" rx="1.5"
              fill="var(--color-muted-foreground)" opacity="0.7" />
      ))}
    </svg>
  );
}
