/**
 * What this workspace has recorded, newest first.
 *
 * The page this replaces had three controls that did nothing. Filter opened
 * nothing, Export downloaded nothing, and Next paged nowhere -- while
 * `/audit-logs` had accepted `entity_type`, `severity` and `limit` from the
 * start and the client had never sent one. So the fix is not decoration: the
 * controls that can be honoured are wired to the parameters the server already
 * reads, and the one that cannot be (Previous/Next -- there is no `offset`) is
 * gone rather than left as a button that teaches people it does nothing.
 *
 * Two claims were also deleted. The old header said the trail was "immutable"
 * and "retained 7 years"; nothing in the API reports either, so both were
 * decoration on a compliance surface, which is the worst place to put it. And
 * the timestamp column was labelled UTC while rendering `toLocaleString()`,
 * i.e. the reader's own zone -- a mislabelled time on an audit record is a
 * defect, not a nit.
 *
 * **There is no hash chain here, so nothing draws one.** No seal, no tick, no
 * link glyph between rows. The spine is a reading aid for a chronological list
 * and is drawn as one -- an unbroken line between entries would be read by
 * exactly the audience this page is for as a tamper-evidence claim, and we
 * cannot make that claim.
 */

import { createFileRoute } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { Download, Search, SlidersHorizontal, X } from "lucide-react";

import { api, type AuditEntry } from "@/lib/api";
import { Input } from "@/components/ui/input";
import { ErrorBanner } from "@/components/error-banner";
import { PolishedEmpty, SkeletonBar } from "@/components/skeletons";
import { plainly } from "@/components/processing-banner";
import { DUR, EASE_OUT, staggerDelay, useCountUp, useReducedMotionFlag } from "@/components/motion";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/_app/audit-log")({
  head: () => ({
    meta: [
      { title: "Audit Log — DocuMind AI" },
      { name: "description", content: "Every action this workspace has recorded, newest first." },
    ],
  }),
  component: AuditLogPage,
});

/* ------------------------------------------------------------------ vocabulary */

/**
 * Tones for the severities we have a colour for.
 *
 * This map is a palette, not a vocabulary, and the distinction is the whole
 * point: `log_audit` takes `severity` as a free string, so the writers can
 * record one nobody here anticipated. When they do, the entry is still drawn --
 * in the neutral tone below -- under **its own name**. What it must never be is
 * relabelled. A row that reads "info" for an entry the server recorded as
 * something else is a claim about the record invented by the screen, and this
 * is the one page where that is unforgivable.
 *
 * `error` and `failure` share the blocked tone: there is no "critical" tier in
 * the data, and inventing a third red would be inventing a severity the server
 * cannot produce.
 */
const SEVERITY_TONE: Record<string, string> = {
  info: "border-ai-idle/35 bg-ai-idle/10 text-ai-idle",
  success: "border-ai-confident/35 bg-ai-confident/10 text-ai-confident",
  warning: "border-ai-uncertain/40 bg-ai-uncertain/10 text-ai-uncertain",
  error: "border-ai-blocked/40 bg-ai-blocked/10 text-ai-blocked",
  failure: "border-ai-blocked/40 bg-ai-blocked/10 text-ai-blocked",
};

/** The dot on the spine. Colour is never the only carrier -- every row prints
 *  its severity as a word as well -- so this is emphasis, not information. */
const SEVERITY_DOT: Record<string, string> = {
  info: "bg-ai-idle",
  success: "bg-ai-confident",
  warning: "bg-ai-uncertain",
  error: "bg-ai-blocked",
  failure: "bg-ai-blocked",
};

/** For a severity the palette has no entry for. Neutral on purpose: we do not
 *  know what an unrecognised severity means, and picking a red or a green for
 *  it would be asserting that we do. */
const UNKNOWN_TONE = "border-border-strong bg-accent text-muted-foreground";
const UNKNOWN_DOT = "bg-muted-foreground/60";

/** Styling only, both of these. The word a row prints, and the word it exports,
 *  is always the string the server stored -- never the key we fell back to. */
function severityTone(raw: string): string {
  return SEVERITY_TONE[(raw ?? "").toLowerCase()] ?? UNKNOWN_TONE;
}

function severityDot(raw: string): string {
  return SEVERITY_DOT[(raw ?? "").toLowerCase()] ?? UNKNOWN_DOT;
}

/** Escalation order for the severities we recognise, so a list derived from the
 *  data still reads in the order a reader expects. Anything unrecognised sorts
 *  after them, alphabetically -- we have no basis for ranking it. */
const SEVERITY_RANK: Record<string, number> = {
  info: 0, success: 1, warning: 2, error: 3, failure: 4,
};

function bySeverity(a: string, b: string): number {
  const ra = SEVERITY_RANK[a] ?? 99;
  const rb = SEVERITY_RANK[b] ?? 99;
  return ra === rb ? a.localeCompare(b) : ra - rb;
}

/**
 * Entity types in the product's words.
 *
 * The stored values are the engine's own names -- `template_manifest`,
 * `template_blueprint` -- and a person who uploaded a Word file has no reason
 * to meet either. `plainly()` cannot help here: its word-boundary rules do not
 * fire inside `template_manifest`, so this is the explicit map that job needs.
 * Anything unmapped falls back to the raw key with its underscores opened out,
 * which is ugly but never wrong.
 */
const ENTITY_LABEL: Record<string, string> = {
  project: "Project",
  template_file: "Template upload",
  template_blueprint: "Template",
  template_manifest: "Template rules",
  template_cluster: "Template group",
  source_file: "Source data",
  generated_document: "Document",
  document_version: "Document version",
  document_review: "Review",
  review_task: "Review question",
  generation_job: "Batch run",
  org_model_rate: "AI pricing",
  organization: "Workspace",
};

function entityLabel(raw: string): string {
  if (!raw) return "—";
  // Unmapped keys are internal names, so they get a generic word.
  return ENTITY_LABEL[raw] ?? "Record";
}

/**
 * Events whose stored wording carries engine vocabulary into a sentence a
 * customer reads.
 *
 * Rewritten here rather than at the writer, for the same reason
 * `processing-banner` rewrites stage labels on the client: those strings are
 * the audit record, and renaming them for the benefit of a screen would edit
 * something somebody may one day have to defend. Presentation is the right
 * place for a presentation problem. Everything not listed goes through
 * `plainly()`, which is enough for the rest.
 */
const EVENT_LABEL: Record<string, string> = {
  "Compiled template manifest": "Read a template",
  "Compiled manifest left unapproved": "Read a template, left unapproved",
  "Published manifest left unapproved": "Published a template, left unapproved",
  "Approved template manifest": "Approved a template",
  "Generated document from manifest": "Generated a document from a template",
  "Created a template blueprint from an upload": "Created a template from an upload",
  "Saved a template blueprint": "Saved a template",
  "Deleted a template blueprint": "Deleted a template",
  "Archived a template blueprint": "Archived a template",
  "Reverted a template blueprint": "Reverted a template",
  "Emitted a template from a blueprint": "Exported a template as a file",
  "Published a template from a blueprint": "Published a template",
  "Migrated a token-library template": "Migrated an older template",
  "data_policy.updated": "Updated the data policy",
  "retention.sweep": "Applied the retention schedule",
  "compiled (no sample data to verify against)": "Read a template",
};

function eventLabel(raw: string): string {
  if (EVENT_LABEL[raw]) return EVENT_LABEL[raw];
  // Single-token machine keys are internal; stored sentences are already
  // written for a reader and only need scrubbing.
  if (!raw || !/\s/.test(raw.trim())) return "Activity";
  return plainly(raw);
}

/* ---------------------------------------------------------------------- time */

/** Relative for scanning, absolute on hover for quoting. Deliberately not
 *  labelled UTC anywhere: this is the reader's own zone, which is what
 *  `toLocaleString` returns and what the old header got wrong. */
function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 45) return "just now";
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86_400) return `${Math.round(secs / 3600)}h ago`;
  if (secs < 604_800) return `${Math.round(secs / 86_400)}d ago`;
  return new Date(then).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function absoluteTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/* ------------------------------------------------------------------ grouping */

/** An unbroken run of actions by one actor. Runs rather than a global regroup:
 *  collecting every action a person ever took under one heading would reorder a
 *  chronological record, and the order is half of what an audit trail says. */
type Run = { actor: string | null; entries: AuditEntry[] };

function runsByActor(entries: AuditEntry[]): Run[] {
  const runs: Run[] = [];
  for (const entry of entries) {
    const last = runs[runs.length - 1];
    if (last && last.actor === entry.actor) last.entries.push(entry);
    else runs.push({ actor: entry.actor, entries: [entry] });
  }
  return runs;
}

function initials(name: string): string {
  return name.split(" ").filter(Boolean).map((n) => n[0]).join("").slice(0, 2).toUpperCase();
}

/* ------------------------------------------------------------------- the page */

/** How far back a request may reach. There is no `offset` on the endpoint, so
 *  `limit` is the only lever, and "Show more" raises it rather than pretending
 *  to turn a page. */
const LIMITS = [50, 100, 250, 500] as const;

function AuditLogPage() {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [entityType, setEntityType] = useState<string>("");
  const [severity, setSeverity] = useState<string>("");
  const [limitIndex, setLimitIndex] = useState(0);
  const [query, setQuery] = useState("");

  const limit = LIMITS[limitIndex];

  /**
   * Entity types and severities learned from what the log has actually
   * returned.
   *
   * Both unions only ever grow -- filtering to a value can only return that
   * value -- so the lists are stable across filter changes while never offering
   * something this workspace has no record of.
   *
   * Severity is derived rather than listed for a reason found the hard way. The
   * fixed list here used to offer info | success | warning | error | failure,
   * and an exhaustive read of every `log_audit` call in the backend produces
   * exactly two of them: the `severity: str = "info"` default, and an explicit
   * `severity="warning"` at eight sites. Three of those five chips could only
   * ever return an empty list -- a control that teaches the reader this
   * workspace has no errors, when what it really shows is that nothing writes
   * that word. Offering what the record contains cannot lie that way, and it
   * picks up any severity a future writer introduces without an edit here.
   */
  const [seenEntityTypes, setSeenEntityTypes] = useState<string[]>([]);
  const [seenSeverities, setSeenSeverities] = useState<string[]>([]);

  const load = useCallback((params: { entity_type?: string; severity?: string; limit: number }) => {
    setLoading(true);
    api.auditLogEntries({
      entity_type: params.entity_type || undefined,
      severity: params.severity || undefined,
      limit: params.limit,
    })
      .then((r) => {
        const items = r.items ?? [];
        setEntries(items);
        setSeenEntityTypes((known) => {
          const merged = new Set(known);
          for (const item of items) if (item.entity_type) merged.add(item.entity_type);
          return [...merged].sort();
        });
        setSeenSeverities((known) => {
          const merged = new Set(known);
          for (const item of items) if (item.severity) merged.add(item.severity);
          return [...merged].sort(bySeverity);
        });
        setError(null);
      })
      // A banner rather than a toast: this request is the entire page, so a
      // failure is the state of the screen and not a passing remark about it.
      .catch((e: any) => setError(e?.message ?? String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load({ entity_type: entityType, severity, limit });
  }, [entityType, severity, limit, load]);

  const filtered = useMemo(() => {
    const list = entries ?? [];
    const q = query.trim().toLowerCase();
    if (!q) return list;
    // Over what is loaded, and the count line says so. The endpoint has no
    // search parameter, and a box that quietly searched a fiftieth of the
    // record while looking like it searched all of it would be worse than none.
    return list.filter((e) =>
      [e.actor ?? "", eventLabel(e.action), e.target ?? "", entityLabel(e.entity_type)]
        .join(" ").toLowerCase().includes(q));
  }, [entries, query]);

  const runs = useMemo(() => runsByActor(filtered), [filtered]);
  const actorCount = useMemo(
    () => new Set(filtered.map((e) => e.actor ?? "unattributed")).size,
    [filtered],
  );

  const filtersOn = Boolean(entityType || severity || query.trim());
  // The server returned fewer rows than we asked for, so there is nothing
  // further back to fetch -- the only honest basis for disabling "Show more".
  const reachedEnd = entries != null && entries.length < limit;
  const canShowMore = !reachedEnd && limitIndex < LIMITS.length - 1;

  function clearFilters() {
    setEntityType("");
    setSeverity("");
    setQuery("");
  }

  return (
    <div className="mx-auto max-w-6xl space-y-5 p-6 lg:p-8">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: DUR.reveal, ease: EASE_OUT }}
        className="flex flex-wrap items-start justify-between gap-4"
      >
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-gradient">Audit Log</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Every action this workspace has recorded, newest first.
          </p>
        </div>
        <ExportButton entries={filtered} />
      </motion.div>

      <FilterBar
        query={query}
        onQuery={setQuery}
        entityType={entityType}
        onEntityType={(v) => { setEntityType(v); setLimitIndex(0); }}
        entityTypes={seenEntityTypes}
        severity={severity}
        onSeverity={(v) => { setSeverity(v); setLimitIndex(0); }}
        severities={seenSeverities}
        loading={loading}
      />

      {error && (
        <ErrorBanner
          title="Could not load the audit log"
          message={entries?.length
            ? "The entries below are the last set that did load."
            : "Nothing was read from the server. The record itself is unaffected."}
          detail={error}
          onRetry={() => load({ entity_type: entityType, severity, limit })}
          retrying={loading}
        />
      )}

      {/* Nothing has loaded and nothing is loading means the request failed, and
          the banner above is already saying so. The empty state must not run in
          that case: "nothing has been recorded yet" is a statement about the
          record, and we have not read the record. */}
      {entries == null ? (
        loading ? <TimelineSkeleton /> : null
      ) : filtered.length === 0 ? (
        <PolishedEmpty
          icon={<SlidersHorizontal className="h-5 w-5" />}
          title={filtersOn ? "No entries match these filters" : "Nothing has been recorded yet"}
          subtitle={filtersOn
            ? "Nothing in the entries loaded matches what you asked for. Widen the filters, or reach further back."
            : "Actions are recorded as people take them. This page fills itself."}
          action={filtersOn ? (
            <button
              onClick={clearFilters}
              className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-surface px-3 text-[12.5px] font-medium transition-colors hover:bg-accent"
            >
              <X className="h-3.5 w-3.5" /> Clear filters
            </button>
          ) : undefined}
        />
      ) : (
        <>
          <SummaryLine
            shown={filtered.length}
            loaded={entries?.length ?? 0}
            actors={actorCount}
            searching={Boolean(query.trim())}
          />
          <Timeline runs={runs} revealKey={`${entityType}|${severity}|${limit}`} />
          <div className="flex items-center justify-between gap-3 pt-1 text-xs text-muted-foreground">
            <span>
              {reachedEnd
                ? "This is everything on record."
                : `The ${limit} most recently recorded actions.`}
            </span>
            {canShowMore && (
              <button
                onClick={() => setLimitIndex((i) => Math.min(i + 1, LIMITS.length - 1))}
                disabled={loading}
                className="inline-flex h-8 items-center rounded-lg border border-border bg-surface px-3 text-[12.5px] font-medium text-foreground transition-colors hover:bg-accent disabled:opacity-60"
              >
                {loading ? "Loading…" : `Show ${LIMITS[limitIndex + 1]}`}
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------- controls */

function FilterBar({
  query, onQuery, entityType, onEntityType, entityTypes, severity, onSeverity, severities, loading,
}: {
  query: string;
  onQuery: (v: string) => void;
  entityType: string;
  onEntityType: (v: string) => void;
  entityTypes: string[];
  severity: string;
  onSeverity: (v: string) => void;
  severities: string[];
  loading: boolean;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: DUR.reveal, ease: EASE_OUT, delay: 0.04 }}
      className="rounded-xl surface-raised p-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[220px] flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => onQuery(e.target.value)}
            placeholder="Search the entries loaded…"
            className="pl-9"
            aria-label="Search the entries loaded"
          />
        </div>

        {/* Offered only once the log has shown us more than one kind of entity
            -- a select with a single option is a control that cannot change
            anything, which is the thing this page had too much of. */}
        {entityTypes.length > 1 && (
          <select
            value={entityType}
            onChange={(e) => onEntityType(e.target.value)}
            disabled={loading}
            aria-label="Filter by what was acted on"
            className="h-9 rounded-lg border border-border bg-background px-3 text-sm disabled:opacity-50"
          >
            <option value="">Everything</option>
            {entityTypes.map((t) => (
              <option key={t} value={t}>{entityLabel(t)}</option>
            ))}
          </select>
        )}
      </div>

      {/* Shown once the log has returned more than one severity, on the same
          rule as the entity select above: a row whose only chip selects every
          row already on screen is a control that cannot change anything. */}
      {severities.length > 1 && (
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
          <span className="mr-1 text-[11px] uppercase tracking-wider text-muted-foreground">Severity</span>
          <SeverityChip active={severity === ""} onClick={() => onSeverity("")} tone="">Any</SeverityChip>
          {severities.map((s) => (
            <SeverityChip key={s} active={severity === s} onClick={() => onSeverity(severity === s ? "" : s)} tone={s}>
              {s}
            </SeverityChip>
          ))}
        </div>
      )}
    </motion.div>
  );
}

function SeverityChip({ active, onClick, tone, children }: {
  active: boolean;
  onClick: () => void;
  tone: string;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "rounded-md border px-2 py-0.5 text-[11px] font-medium capitalize transition-colors duration-150",
        active
          ? tone
            ? severityTone(tone)
            : "border-border-strong bg-accent text-foreground"
          : "border-transparent text-muted-foreground hover:bg-accent/60 hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

/** Downloads exactly the rows on screen, in the words on screen.
 *
 *  Two decisions worth stating. It exports the *filtered, loaded* set, because
 *  that is the only set this page holds -- there is no bulk export endpoint, so
 *  a button promising the whole record would be promising something no request
 *  behind it could deliver. And it exports the labels a person just read rather
 *  than the stored strings, so the file matches the screen and the engine's
 *  internal names do not travel out in a spreadsheet. The raw timestamp goes in
 *  ISO, which is the one column a machine may want back. */
function ExportButton({ entries }: { entries: AuditEntry[] }) {
  function download() {
    if (!entries.length) return;
    const cell = (v: string) => `"${(v ?? "").replace(/"/g, '""')}"`;
    const rows = [
      ["Recorded at (ISO)", "Recorded at (local)", "Actor", "Action", "What", "Target", "Severity"],
      ...entries.map((e) => [
        e.time ?? "",
        absoluteTime(e.time),
        e.actor ?? "Not recorded",
        eventLabel(e.action),
        entityLabel(e.entity_type),
        e.target ?? "",
        // The stored word, verbatim. A spreadsheet column headed "Severity" is
        // quoted back at people; it carries whatever the record carries.
        e.severity ?? "",
      ]),
    ];
    const csv = rows.map((r) => r.map(cell).join(",")).join("\r\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `audit-log-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    // Deferred a tick: revoking in the same frame as the click has been seen to
    // cancel the download before the browser has read the blob.
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  return (
    <button
      onClick={download}
      disabled={entries.length === 0}
      title="Downloads the entries listed below, as CSV"
      className="inline-flex h-9 items-center gap-2 rounded-lg border border-border bg-surface px-3 text-sm font-medium transition-colors hover:bg-accent disabled:opacity-50"
    >
      <Download className="h-4 w-4" />
      Export shown
    </button>
  );
}

function SummaryLine({ shown, loaded, actors, searching }: {
  shown: number; loaded: number; actors: number; searching: boolean;
}) {
  // Counted up because it is a real measurement of a real set. Gated on there
  // being one: this line does not render at all when nothing matched, so the
  // counter can never animate toward a zero that means "unmeasured".
  const counted = Math.round(useCountUp(shown, 500, shown > 0));
  return (
    <p className="px-1 text-xs text-muted-foreground">
      <span className="tabular-nums text-foreground">{counted}</span>
      {searching ? ` of ${loaded} loaded ${loaded === 1 ? "entry" : "entries"}` : ` ${shown === 1 ? "entry" : "entries"}`}
      {" · "}
      <span className="tabular-nums text-foreground">{actors}</span>
      {actors === 1 ? " actor" : " actors"}
    </p>
  );
}

/* ------------------------------------------------------------------- timeline */

/**
 * The spine draws once per result set.
 *
 * `scaleY` from a top origin, never a height: height is laid out and painted
 * every frame, a transform is composited, and this line can be a thousand
 * pixels tall. `revealKey` remounts it when the filters change, so a new result
 * set draws itself rather than appearing under an already-finished line.
 */
function Timeline({ runs, revealKey }: { runs: Run[]; revealKey: string }) {
  const reduced = useReducedMotionFlag();
  // A flat counter across every run, so the 30ms step is capped at twelve items
  // for the page rather than restarting inside each actor's block.
  let flatIndex = -1;

  return (
    <div className="relative rounded-xl surface-raised p-4 sm:p-5">
      <motion.span
        key={revealKey}
        aria-hidden
        initial={reduced ? false : { scaleY: 0 }}
        animate={{ scaleY: 1 }}
        transition={{ duration: DUR.revealSlow, ease: EASE_OUT }}
        style={{
          transformOrigin: "top",
          // Written out rather than assembled from `from-*`/`via-*` utilities
          // so the stops are explicit: a two-stop fade over the whole line
          // leaves most of a fifty-entry list with no visible spine at all. It
          // holds until the last tenth, then lets go.
          backgroundImage:
            "linear-gradient(to bottom, var(--color-border-strong) 0%, var(--color-border) 10%," +
            " var(--color-border) 88%, transparent 100%)",
        }}
        className="pointer-events-none absolute bottom-6 left-[30px] top-[30px] w-px sm:left-[34px] sm:top-[34px]"
      />

      <div className="relative space-y-4">
        {runs.map((run, r) => (
          <div key={`${run.actor ?? "unattributed"}-${run.entries[0]?.id ?? r}`}>
            <ActorHeading run={run} index={flatIndex + 1} reduced={reduced} />
            <div className="mt-1 space-y-0.5">
              {run.entries.map((entry) => {
                flatIndex += 1;
                return <EntryRow key={entry.id} entry={entry} index={flatIndex} reduced={reduced} />;
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function ActorHeading({ run, index, reduced }: { run: Run; index: number; reduced: boolean }) {
  const name = run.actor;
  return (
    <motion.div
      initial={reduced ? false : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: DUR.base, ease: EASE_OUT, delay: staggerDelay(index) }}
      className="flex items-center gap-3"
    >
      <span
        className={cn(
          "z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-[10px] font-semibold",
          name
            ? "border-border-strong bg-surface-elevated text-foreground"
            : "border-dashed border-border bg-surface text-muted-foreground",
        )}
      >
        {name ? initials(name) : "—"}
      </span>
      <span className="text-sm font-medium">
        {name ?? (
          <span
            className="text-muted-foreground"
            title="These entries were recorded without an actor name."
          >
            Actor not recorded
          </span>
        )}
      </span>
      <span className="text-[11px] tabular-nums text-muted-foreground">
        {run.entries.length} {run.entries.length === 1 ? "action" : "actions"}
      </span>
    </motion.div>
  );
}

function EntryRow({ entry, index, reduced }: { entry: AuditEntry; index: number; reduced: boolean }) {
  const sev = (entry.severity ?? "").trim();
  return (
    <motion.div
      initial={reduced ? false : { opacity: 0, x: -6 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: DUR.base, ease: EASE_OUT, delay: staggerDelay(index) }}
      className="group flex items-start gap-3 rounded-lg py-1.5 pl-[9px] pr-2 transition-colors duration-150 hover:bg-accent/40"
    >
      <span className="relative mt-[10px] flex h-2.5 w-2.5 shrink-0 items-center justify-center">
        <span className={cn("h-2 w-2 rounded-full ring-2 ring-surface", severityDot(sev))} />
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="text-[13px] leading-snug text-foreground">{eventLabel(entry.action)}</span>
          <span className="text-[11px] text-muted-foreground">{entityLabel(entry.entity_type)}</span>
        </div>
        {entry.target && (
          // Verbatim, unlike the event sentence. A target is a name somebody
          // chose -- a project, a filename, a row count -- and rewording it
          // would misreport which thing the action touched.
          <p className="mt-0.5 truncate text-[11px] text-muted-foreground/80" title={entry.target}>
            {entry.target}
          </p>
        )}
      </div>

      {sev ? (
        // The stored severity, not a key we recognised. An unrecognised word
        // gets the neutral tone and keeps its own name; it is never redrawn as
        // one of the five we happen to have a colour for.
        <span
          className={cn(
            "mt-0.5 shrink-0 rounded border px-1.5 py-px text-[10px] font-medium uppercase tracking-wide",
            severityTone(sev),
          )}
        >
          {sev}
        </span>
      ) : (
        <span
          className="mt-0.5 shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground"
          title="This entry was recorded without a severity."
        >
          —
        </span>
      )}
      <span
        className="mt-0.5 w-16 shrink-0 text-right text-[11px] tabular-nums text-muted-foreground"
        title={absoluteTime(entry.time)}
      >
        {relativeTime(entry.time)}
      </span>
    </motion.div>
  );
}

/** The timeline at the size it will be, spine included, so the entries land
 *  into a layout that was already standing. */
function TimelineSkeleton() {
  const rows = [3, 2, 4];
  return (
    <div className="relative rounded-xl surface-raised p-4 sm:p-5">
      <span
        aria-hidden
        className="pointer-events-none absolute bottom-6 left-[30px] top-[30px] w-px bg-border sm:left-[34px] sm:top-[34px]"
      />
      <div className="relative space-y-4">
        {rows.map((count, r) => (
          <div key={r}>
            <div className="flex items-center gap-3">
              <SkeletonBar className="h-7 w-7 shrink-0 rounded-full" />
              <SkeletonBar className="h-3 w-32" />
            </div>
            <div className="mt-2 space-y-2">
              {Array.from({ length: count }).map((_, i) => (
                <div key={i} className="flex items-center gap-3 pl-[10px]">
                  <SkeletonBar className="h-2 w-2 shrink-0 rounded-full" />
                  <SkeletonBar className={cn("h-3", i % 2 ? "w-64" : "w-80")} />
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
