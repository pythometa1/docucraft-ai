import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Boxes,
  Braces,
  Clock,
  FilePlus2,
  FileSymlink,
  GitBranch,
  LayoutTemplate,
  Library,
  Loader2,
  Plus,
  ScanLine,
  Search,
  Sparkles,
  Trash2,
  Wand2,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { BlueprintImportDialog } from "@/components/blueprint-import-dialog";
import { plainly } from "@/components/processing-banner";
import { BlueprintKitDialog } from "@/components/blueprint-kit-dialog";
import { DeleteTemplateDialog } from "@/components/delete-template-dialog";
import { BulkSelectBar, SelectBox, useSelection, type Selection } from "@/components/bulk-select";
import { FadeIn, Stagger, StaggerItem, useCountUp } from "@/components/motion";
import { CardGridSkeleton, PolishedEmpty } from "@/components/skeletons";
import { ErrorBanner } from "@/components/error-banner";
import { cn } from "@/lib/utils";
import type { Blueprint } from "@/lib/types";

export const Route = createFileRoute("/_app/templates")({
  head: () => ({
    meta: [
      { title: "Templates — DocuMind AI" },
      { name: "description", content: "Turn an existing Word template into an editable one, review what was found, and publish it ready to generate documents." },
      { property: "og:title", content: "Template authoring — DocuMind AI" },
      { property: "og:description", content: "Your existing templates, ready to generate documents." },
    ],
  }),
  component: TemplatesPage,
});

/* ---------------------------------------------------------------------------
   Card vocabulary.

   Two independent things are being shown, and they are drawn with two different
   instruments on purpose. The *glyph* says where a template came from; the
   *colour* says what state it is in. Colouring by origin as well would leave a
   published kit green in one place and violet in another, and the reader would
   have no way to know which of the two the colour was answering.
   --------------------------------------------------------------------------- */

/** Where the template came from, in the words of the person who put it there --
 *  never the value of `kind`, which is our column and not their vocabulary. */
const KIND: Record<Blueprint["kind"], { icon: LucideIcon; origin: string }> = {
  legacy: { icon: ScanLine, origin: "Read from a Word document" },
  inherited: { icon: FileSymlink, origin: "Carried over from a project" },
  kit: { icon: LayoutTemplate, origin: "Started from a kit" },
  library: { icon: Library, origin: "Migrated from the old template library" },
  blank: { icon: FilePlus2, origin: "Started from a blank page" },
};

const KIND_FALLBACK = { icon: Boxes, origin: "Template" } as const;

/** The status palette, drawn from the shared ai-* tokens so a published
 *  template is the same green here as a confident reading is everywhere else. */
const STATUS: Record<Blueprint["status"], {
  label: string; ribbon: string; halo: string; tile: string; chip: string; dot: string;
}> = {
  published: {
    label: "Published",
    ribbon: "bg-ai-confident/70",
    halo: "bg-ai-confident/20",
    tile: "border-ai-confident/40 bg-ai-confident/12 text-ai-confident",
    chip: "border-ai-confident/35 bg-ai-confident/10 text-ai-confident",
    dot: "bg-ai-confident",
  },
  draft: {
    label: "Draft",
    ribbon: "bg-ai-active/70",
    halo: "bg-ai-active/20",
    tile: "border-ai-active/40 bg-ai-active/12 text-ai-active",
    chip: "border-ai-active/35 bg-ai-active/10 text-ai-active",
    dot: "bg-ai-active",
  },
  archived: {
    label: "Archived",
    ribbon: "bg-ai-idle/60",
    halo: "bg-ai-idle/15",
    tile: "border-ai-idle/40 bg-ai-idle/10 text-ai-idle",
    chip: "border-ai-idle/35 bg-ai-idle/10 text-ai-idle",
    dot: "bg-ai-idle",
  },
};

/** One chip shape for the whole card, so the status pill and the version pill
 *  sit on the same baseline without the utility string being typed twice. */
const CHIP = "inline-flex items-center gap-1.5 rounded-md border px-1.5 py-0.5 text-[10.5px] font-medium leading-none";

// Stable across renders: useSelection memoises on these.
const blueprintId = (b: Blueprint) => b.id;
const anyTemplate = () => true;

/** Remove several templates, one request each (there is no bulk endpoint).
 *  Drafts are deleted. A published template the server refuses to delete
 *  (BLUEPRINT_IN_USE) is archived instead -- the same end state the single
 *  delete offers: off the list, with everything generated from it unchanged. */
async function removeTemplates(ids: string[]) {
  const deleted: string[] = [];
  const archived: string[] = [];
  const refused: { reason: string }[] = [];
  for (const id of ids) {
    try {
      await api.deleteBlueprint(id);
      deleted.push(id);
    } catch (e: any) {
      if (e?.code !== "BLUEPRINT_IN_USE") {
        refused.push({ reason: plainly(e?.message ?? String(e)) });
        continue;
      }
      try {
        await api.archiveBlueprint(id);
        archived.push(id);
      } catch (e2: any) {
        refused.push({ reason: plainly(e2?.message ?? String(e2)) });
      }
    }
  }
  return { deleted, archived, refused };
}

type SortKey = "updated" | "name" | "created";

const SORTS: { key: SortKey; label: string }[] = [
  { key: "updated", label: "Last updated" },
  { key: "name", label: "Name" },
  { key: "created", label: "Newest" },
];

/* ---------------------------------------------------------------------------
   Dates.

   Both helpers take whatever the row actually holds rather than what the type
   promises: `created_at` and `updated_at` are declared as strings, but a row
   written before those columns existed comes back null, and `new Date(null)` is
   an Invalid Date that renders as "NaN days ago" instead of throwing.
   --------------------------------------------------------------------------- */

function timestamp(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isFinite(t) ? t : null;
}

/** "just now", "4 minutes ago", "3 days ago". Null when there is no usable
 *  date, so the caller can say so rather than printing a broken sentence.
 *
 *  A timestamp slightly in the future -- which is what clock skew between the
 *  server and this machine looks like -- reads as "just now" rather than as a
 *  negative interval. */
function relativeTime(iso: string | null | undefined): string | null {
  const t = timestamp(iso);
  if (t == null) return null;

  // Anything under a minute is "just now" rather than a count: the smallest
  // unit below is the minute, and floor-dividing 50 seconds by it would print
  // "0 minutes ago".
  const secs = Math.round((Date.now() - t) / 1000);
  if (secs < 60) return "just now";

  const units: [number, string][] = [
    [60, "minute"],
    [3600, "hour"],
    [86400, "day"],
    [604800, "week"],
    [2592000, "month"],
    [31536000, "year"],
  ];

  let chosen = units[0];
  for (const unit of units) if (secs >= unit[0]) chosen = unit;

  const n = Math.floor(secs / chosen[0]);
  return `${n} ${chosen[1]}${n === 1 ? "" : "s"} ago`;
}

/** The full date, for the `title` on the relative one. Somebody reconciling a
 *  change against an audit trail needs the actual time, and "3 days ago" is not
 *  a time. */
function absoluteTime(iso: string | null | undefined): string | undefined {
  const t = timestamp(iso);
  return t == null ? undefined : new Date(t).toLocaleString();
}

function TemplatesPage() {
  const navigate = useNavigate();
  const [blueprints, setBlueprints] = useState<Blueprint[]>([]);
  const [legacy, setLegacy] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<SortKey>("updated");
  const [importOpen, setImportOpen] = useState(false);
  const [kitOpen, setKitOpen] = useState(false);
  const [migrating, setMigrating] = useState<string | null>(null);
  // The one template whose delete dialog is open, if any.
  const [toDelete, setToDelete] = useState<{ id: string; name: string } | null>(null);

  const refresh = () => {
    setLoading(true);
    setLoadError(null);
    Promise.all([
      // Swallowed so one dead endpoint cannot blank the other list, but the
      // message is kept rather than dropped: an empty grid and a failed request
      // look identical on screen and mean opposite things.
      api.listBlueprints().catch((e: any) => {
        setLoadError(plainly(e?.message ?? String(e)));
        return { items: [] as Blueprint[] };
      }),
      // The old token library. Still readable so nobody's authoring work
      // disappears, and no longer the way new templates are made.
      api.listLibrary().catch(() => ({ items: [] as any[] })),
    ])
      .then(([bp, lib]) => { setBlueprints(bp.items); setLegacy(lib.items ?? []); })
      .catch((e: any) => toast.error("Could not load templates", { description: plainly(e?.message ?? "") }))
      .finally(() => setLoading(false));
  };

  useEffect(refresh, []);

  const list = useMemo(() => {
    const matched = blueprints.filter((b) => b.name.toLowerCase().includes(q.toLowerCase()));
    // Sorted on a copy: the array in state is what the next render filters, and
    // sorting in place would quietly reorder it under React.
    return matched.slice().sort((a, b) => {
      if (sort === "name") return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
      const field = sort === "created" ? "created_at" : "updated_at";
      // Undated rows sort last rather than jumping to the top, which is where a
      // NaN comparison would otherwise leave them.
      return (timestamp(b[field]) ?? 0) - (timestamp(a[field]) ?? 0);
    });
  }, [blueprints, q, sort]);

  // Over what is on screen, so a search narrows what "Select all" ticks.
  const selection = useSelection(list, blueprintId, anyTemplate);
  const chosenNames = list.filter((b) => selection.has(b.id)).map((b) => b.name);

  return (
    <div className="mx-auto max-w-[1400px] space-y-5 p-6 lg:p-8">
      <FadeIn className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2.5">
            <h1 className="text-2xl font-semibold tracking-tight text-gradient">Templates</h1>
            {!loading && blueprints.length > 0 && (
              <span className={cn(CHIP, "border-border bg-surface-elevated text-muted-foreground")}>
                <CountUp value={blueprints.length} /> total
              </span>
            )}
          </div>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            Upload an existing <code className="text-xs">.docx</code> and its placeholders, author
            instructions and conditional sections are identified for you. Review and adjust anything
            you like, download the document whenever you need it, and publish it as a template ready
            to generate documents.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => setKitOpen(true)} className="gap-2">
            <Plus className="h-4 w-4" /> Start from scratch
          </Button>
          <Button onClick={() => setImportOpen(true)} className="gap-2 sheen">
            <span aria-hidden className="sheen-layer" />
            <Wand2 className="h-4 w-4" /> Read a legacy template
          </Button>
        </div>
      </FadeIn>

      {/* What each kind of template text does in the finished document. */}
      <FadeIn delay={0.04} className="relative overflow-hidden rounded-xl surface-raised">
        <span aria-hidden className="grid-noise pointer-events-none absolute inset-0 opacity-[0.35]" />
        <div className="relative flex flex-wrap items-center gap-x-6 gap-y-2 p-4 text-sm">
          <span className="mr-1 text-xs font-medium uppercase tracking-wider text-muted-foreground">
            Legend
          </span>
          <Legend label="Static text — copied as written" cssVar="--color-foreground" glyph="Aa" />
          <Legend label="Placeholder — filled from your data" cssVar="--color-token-source" Icon={Braces} />
          <Legend label="Author instruction — removed from the letter" cssVar="--color-token-prompt" Icon={Sparkles} />
          <Legend label="Conditional section" cssVar="--color-token-conditional" Icon={GitBranch} />
        </div>
      </FadeIn>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="relative w-full max-w-sm">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search templates…" className="pl-9" />
        </div>

        {/* Sorting is over the list already in state -- no request, so the order
            changes on the same frame the button is pressed. */}
        <div className="flex items-center gap-1.5">
          <span className="mr-0.5 hidden text-xs font-medium uppercase tracking-wider text-muted-foreground sm:inline">
            Sort by
          </span>
          {SORTS.map((s) => (
            <button
              key={s.key}
              onClick={() => setSort(s.key)}
              aria-pressed={sort === s.key}
              className={cn(
                "rounded-lg border px-2.5 py-1 text-xs font-medium transition-colors",
                sort === s.key
                  ? "border-brand bg-brand text-brand-foreground"
                  : "border-border hover:bg-accent",
              )}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>

      {/* A failed load leaves an empty list, and an empty list already has a
          panel that says "no templates yet" -- so the banner replaces that panel
          rather than sitting above it telling the reader two different stories. */}
      {loadError && (
        <ErrorBanner
          title="Could not load your templates"
          message="Some templates may be missing or out of date. Nothing has been changed or deleted — try again in a moment."
          detail={loadError}
          onRetry={refresh}
          retrying={loading}
        />
      )}

      {loading ? (
        <CardGridSkeleton count={6} />
      ) : list.length > 0 ? (
        <>
        <BulkSelectBar
          selection={selection}
          noun="template"
          pluralNoun="templates"
          names={chosenNames}
          description={
            "Drafts are deleted. Templates that have already been published are archived instead: "
            + "they leave this list, and every document generated from them stays exactly as it is."
          }
          onDelete={removeTemplates}
          onDone={refresh}
        />
        <Stagger className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {list.map((b) => (
            <StaggerItem key={b.id} className="h-full">
              <TemplateCard
                blueprint={b}
                onOpen={() => navigate({ to: "/templates/$blueprintId", params: { blueprintId: b.id } })}
                onDelete={() => setToDelete({ id: b.id, name: b.name })}
                selection={selection}
              />
            </StaggerItem>
          ))}
        </Stagger>
        </>
      ) : loadError ? null : q.length > 0 ? (
        <PolishedEmpty
          icon={<Search className="h-5 w-5" />}
          title="No templates match"
          subtitle={`Nothing here matches “${q}”. Try a shorter search, or clear it to see everything.`}
          action={
            <Button variant="outline" onClick={() => setQ("")} className="gap-2">
              Clear search
            </Button>
          }
        />
      ) : (
        <PolishedEmpty
          icon={<Wand2 className="h-5 w-5" />}
          title="No templates yet"
          subtitle="Start with a Word document you already send — an offer letter, a contract, a study report. It is read as it is, placeholders and author instructions and all."
          action={
            <Button onClick={() => setImportOpen(true)} className="gap-2">
              <Wand2 className="h-4 w-4" /> Read a legacy template
            </Button>
          }
        />
      )}

      {legacy.length > 0 && (
        <FadeIn className="rounded-xl border border-ai-uncertain/30 bg-ai-uncertain/[0.06] p-4">
          <p className="mb-1 flex items-center gap-1.5 text-sm font-medium">
            <AlertTriangle className="h-3.5 w-3.5 text-ai-uncertain" />
            {legacy.length} template{legacy.length === 1 ? "" : "s"} in the old template library
          </p>
          <p className="text-xs text-muted-foreground">
            These were made in an older format that cannot generate documents. Migrating one
            turns it into a template you can edit and publish; nothing is deleted, the original
            stays where it is.
          </p>
          <div className="mt-3 space-y-1.5">
            {legacy.map((t: any) => (
              <div key={t.id} className="flex items-center justify-between gap-3 rounded-lg border border-border bg-background/60 px-3 py-2">
                <span className="truncate text-sm">{t.name}</span>
                <Button
                  size="sm" variant="ghost" className="gap-1.5 shrink-0"
                  disabled={migrating != null}
                  onClick={async () => {
                    setMigrating(t.id);
                    try {
                      const bp = await api.blueprintFromLibrary({ library_id: t.id });
                      toast.success("Migrated", { description: t.name });
                      navigate({ to: "/templates/$blueprintId", params: { blueprintId: bp.id } });
                    } catch (e: any) {
                      toast.error("Could not migrate", { description: plainly(e?.message ?? String(e)) });
                    } finally { setMigrating(null); }
                  }}
                >
                  {migrating === t.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ArrowRight className="h-3.5 w-3.5" />}
                  Migrate
                </Button>
              </div>
            ))}
          </div>
        </FadeIn>
      )}

      {toDelete && (
        <DeleteTemplateDialog
          blueprintId={toDelete.id}
          name={toDelete.name}
          open
          onOpenChange={(o) => { if (!o) setToDelete(null); }}
          onGone={refresh}
        />
      )}

      <BlueprintKitDialog
        open={kitOpen}
        onOpenChange={setKitOpen}
        onCreated={(id) => navigate({ to: "/templates/$blueprintId", params: { blueprintId: id } })}
      />

      <BlueprintImportDialog
        open={importOpen}
        onOpenChange={setImportOpen}
        onCreated={(id) => navigate({ to: "/templates/$blueprintId", params: { blueprintId: id } })}
      />
    </div>
  );
}

/** One template, as a card.
 *
 *  The whole thing is a single button rather than a panel with a link in it:
 *  there is exactly one destination, and a card where only part of the surface
 *  opens it is a card people click twice. */
function TemplateCard({ blueprint: b, onOpen, onDelete, selection }: {
  blueprint: Blueprint; onOpen: () => void; onDelete: () => void; selection: Selection;
}) {
  const chosen = selection.has(b.id);
  // Both maps are indexed defensively. `kind` and `status` are unions here, but
  // they are the backend's enums -- a value added there arrives as an unstyled
  // card rather than as an undefined read on the next render.
  const kind = KIND[b.kind] ?? KIND_FALLBACK;
  const status = STATUS[b.status] ?? STATUS.draft;
  const Icon = kind.icon;

  const updated = relativeTime(b.updated_at);

  return (
    // The delete control is a sibling of the card's button, not a child: a
    // button inside a button is invalid, and a click on it must not open the card.
    <div className={cn("relative h-full rounded-xl", chosen && "ring-2 ring-brand")}>
    <button
      onClick={onOpen}
      className={cn(
        "group relative h-full w-full overflow-hidden rounded-xl surface-raised p-4 text-left",
        "transition-all duration-200 hover:-translate-y-0.5 hover:border-brand/50 hover:glow-brand",
      )}
    >
      {/* The state, readable before anything is read: a ribbon down the leading
          edge in the status colour, which survives being glanced at across a
          grid of twelve where a chip does not. */}
      <span aria-hidden className={cn("absolute inset-y-0 left-0 w-[3px]", status.ribbon)} />
      <span aria-hidden className="grid-noise pointer-events-none absolute inset-0 opacity-[0.3]" />

      <div className="relative flex items-start gap-3">
        <span className="relative flex h-11 w-11 shrink-0 items-center justify-center">
          {/* The halo is a blurred block behind the tile rather than a second
              border, so it can brighten on hover without moving anything. */}
          <span
            aria-hidden
            className={cn(
              "absolute -inset-1 rounded-2xl blur-md opacity-60 transition-opacity duration-200 group-hover:opacity-100",
              status.halo,
            )}
          />
          <span
            className={cn(
              "relative flex h-11 w-11 items-center justify-center rounded-xl border transition-transform duration-200 group-hover:scale-105",
              status.tile,
            )}
          >
            <Icon className="h-[18px] w-[18px]" />
          </span>
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex items-start gap-2 pr-7">
            <span className="min-w-0 flex-1 truncate font-medium leading-snug">{b.name}</span>
            <ArrowRight className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground opacity-0 transition-all duration-200 group-hover:translate-x-0.5 group-hover:opacity-100" />
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className={cn(CHIP, status.chip)}>
              <span aria-hidden className={cn("h-1.5 w-1.5 rounded-full", status.dot)} />
              {status.label}
            </span>
            <span className={cn(CHIP, "border-border bg-surface-elevated text-muted-foreground")}>
              v{b.version_no ?? 1}
            </span>
            <span className="truncate text-[11px] text-muted-foreground">{kind.origin}</span>
          </div>
        </div>
      </div>

      <div className="relative mt-3 flex items-center gap-1.5 border-t border-border/60 pt-2.5 pr-8 text-[11.5px] text-muted-foreground">
        <Clock className="h-3 w-3 shrink-0" />
        <span>Last updated</span>
        {updated ? (
          <span className="text-foreground/80" title={absoluteTime(b.updated_at)}>{updated}</span>
        ) : (
          <span>not recorded</span>
        )}
      </div>
    </button>
    {/* Top-right, on the title line; a sibling of the card button like the trash. */}
    <span className="absolute right-4 top-4 flex h-5 items-center">
      <SelectBox id={b.id} selection={selection} label={b.name} />
    </span>
    <button
      type="button"
      onClick={onDelete}
      aria-label={`Delete template ${b.name}`}
      title="Delete template"
      className="absolute bottom-2.5 right-3 flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-destructive/40"
    >
      <Trash2 className="h-3.5 w-3.5" />
    </button>
    </div>
  );
}

/** The total, counted up on arrival. Deliberately driven by the loaded list and
 *  not by the search, so the number is not re-animated on every keystroke. */
function CountUp({ value }: { value: number }) {
  const n = useCountUp(value, 600);
  return <>{Math.round(n)}</>;
}

function Legend({ label, cssVar, glyph, Icon }: {
  label: string; cssVar: string; glyph?: string; Icon?: any;
}) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs">
      <span
        className="inline-flex h-5 min-w-[24px] items-center justify-center rounded px-1 font-mono text-[10px] font-bold"
        style={{
          color: `var(${cssVar})`,
          background: `color-mix(in oklab, var(${cssVar}) 14%, transparent)`,
          border: `1px solid color-mix(in oklab, var(${cssVar}) 35%, transparent)`,
        }}
      >
        {Icon ? <Icon className="h-3 w-3" /> : glyph}
      </span>
      <span className="text-muted-foreground">{label}</span>
    </span>
  );
}
