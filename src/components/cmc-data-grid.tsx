/**
 * The Data Review grid: every number that will appear in the dossier, with
 * where it came from and whether anybody has checked it.
 *
 * This screen exists because of one asymmetry. A wrong sentence in a draft is
 * caught by the person reading it; a wrong NUMBER is not, because it looks
 * exactly like a right one. So no extracted value reaches a document until
 * somebody has seen it beside its source and said so, and the header counts
 * how many still have not.
 *
 * Values are shown as strings, never as numbers. `0.050` is three significant
 * figures and `0.05` is two, and a grid that parsed and re-printed them would
 * quietly turn one into the other -- which is the defect the whole module is
 * arranged to prevent.
 *
 * Paged from the start: a stability programme is fifty batches by thirty
 * tests by eight timepoints by three conditions, and a grid that fetched all
 * of it would be a grid nobody can scroll.
 */

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, Check, CheckCircle2, FileWarning, HelpCircle, Loader2,
  Pencil, ShieldQuestion, X, XCircle,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type {
  CmcBatchRow, CmcDataSummary, CmcDocument, CmcResultRow, CmcTestRow,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorBanner } from "@/components/error-banner";
import { PolishedEmpty, TableSkeleton } from "@/components/skeletons";
import { SwapIn } from "@/components/motion";
import { cn } from "@/lib/utils";

type Tab = "results" | "stability" | "specifications" | "batches" | "conflicts";

type Counts = Record<Tab, number>;

const NO_COUNTS: Counts = {
  results: 0, stability: 0, specifications: 0, batches: 0, conflicts: 0,
};

/** One page. The programmes this grid exists for are fifty batches by thirty
 *  tests by eight timepoints by three conditions; the previous fetch asked for
 *  500 rows, ignored the `total` the server returned beside them, and showed
 *  whatever came back. Values that would be rendered into the dossier were
 *  past the cut and could not be seen, let alone corrected. */
const PAGE_SIZE = 100;

const TABS: [Tab, string][] = [
  ["results", "Release results"],
  ["stability", "Stability"],
  ["specifications", "Specifications"],
  ["batches", "Batches"],
  ["conflicts", "Conflicts"],
];

/** How a value was obtained. The number is evidence, so it is shown as words
 *  a reviewer can act on rather than as a bare score. */
function ConfidenceBadge({ value, verified }: { value: number; verified: boolean }) {
  if (verified) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-success/40 bg-success/10 px-1.5 py-0.5 text-[0.65rem] font-medium text-success">
        <Check className="h-3 w-3" /> Verified
      </span>
    );
  }
  const strong = value >= 0.9;
  return (
    <span className={cn(
      "inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[0.65rem] font-medium",
      strong
        ? "border-warning/40 bg-warning/10 text-warning"
        : "border-destructive/40 bg-destructive/10 text-destructive",
    )}>
      {strong ? "Unchecked" : "Needs a look"}
    </span>
  );
}

function ConformanceChip({ outcome, reason }: { outcome: string; reason: string }) {
  const tone = outcome === "pass"
    ? "border-success/40 bg-success/10 text-success"
    : outcome === "fail"
      ? "border-destructive/40 bg-destructive/10 text-destructive"
      : "border-border bg-muted text-muted-foreground";
  const Icon = outcome === "pass" ? CheckCircle2 : outcome === "fail" ? XCircle : HelpCircle;
  return (
    <span title={reason}
          className={cn("inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[0.65rem] font-medium capitalize", tone)}>
      <Icon className="h-3 w-3" />
      {outcome === "unknown" ? "Not checked" : outcome}
    </span>
  );
}

/** One editable value. The input is text, never `type="number"`: a number
 *  input lets the browser normalise what was typed, and "0.050" typed by a
 *  person must reach the server as "0.050". */
function ValueCell({ row, onSaved }: { row: CmcResultRow; onSaved: (r: CmcResultRow) => void }) {
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function save() {
    if (editing === null) return;
    if (!editing.trim()) {
      toast.error("A result needs a value.");
      return;
    }
    setBusy(true);
    try {
      const saved = await api.cmcCorrectResult(row.id, { value_text: editing, verify: true });
      onSaved({ ...row, ...saved });
      setEditing(null);
    } catch (e: any) {
      toast.error("Could not save this value", { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  }

  if (editing !== null) {
    return (
      <div className="flex items-center gap-1">
        <Input
          autoFocus type="text" className="h-7 w-28 font-mono text-xs"
          value={editing}
          onChange={(e) => setEditing(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
            if (e.key === "Escape") setEditing(null);
          }}
        />
        <button onClick={save} disabled={busy}
                className="rounded p-1 text-success hover:bg-success/10" title="Save and verify">
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
        </button>
        <button onClick={() => setEditing(null)}
                className="rounded p-1 text-muted-foreground hover:bg-accent" title="Cancel">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    );
  }

  return (
    <button
      onClick={() => setEditing(row.value_text)}
      className="group inline-flex items-center gap-1.5 rounded px-1 py-0.5 text-left font-mono text-xs hover:bg-accent"
      title="Correct this value — it is stored exactly as you type it"
    >
      {row.value_text}
      <Pencil className="h-3 w-3 opacity-0 transition-opacity group-hover:opacity-60" />
    </button>
  );
}

export function CmcDataGrid({ cmcProjectId, documents }: {
  cmcProjectId: string;
  documents: CmcDocument[];
}) {
  const [tab, setTab] = useState<Tab>("results");
  const [offset, setOffset] = useState(0);
  const [results, setResults] = useState<CmcResultRow[] | null>(null);
  const [pageTotal, setPageTotal] = useState(0);
  const [conflicts, setConflicts] = useState<CmcResultRow[] | null>(null);
  const [tests, setTests] = useState<CmcTestRow[] | null>(null);
  const [batches, setBatches] = useState<CmcBatchRow[] | null>(null);
  const [counts, setCounts] = useState<Counts>(NO_COUNTS);
  const [summary, setSummary] = useState<CmcDataSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  //: What is typed, and what has been asked for. The filter runs on the
  //: server -- a grid this size only ever holds one page, so a filter applied
  //: in the browser would answer "no matches" for a value that is in the
  //: dossier and simply not on screen.
  const [filter, setFilter] = useState("");
  const [query, setQuery] = useState("");

  const sources = useMemo(
    () => Object.fromEntries(documents.map((d) => [d.id, d.filename])),
    [documents],
  );

  /** Reload everything: the tab counts, the project-wide verification summary
   *  and the current page. Called after any mutation. */
  function load() {
    setReload((n) => n + 1);
  }

  // Typing is debounced into `query`, and a new search starts at the first
  // page -- staying on page 4 of a result set that now has one page shows an
  // empty grid over a filter that matched.
  useEffect(() => {
    const timer = setTimeout(() => setQuery(filter.trim()), 300);
    return () => clearTimeout(timer);
  }, [filter]);
  useEffect(() => { setOffset(0); }, [tab, query]);

  // The counts beside each tab, and the gate banner. Deliberately separate
  // from the page fetch: the banner is about the whole project and must not
  // change because somebody typed in the filter box or turned a page.
  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const [all, rel, stab, spec, bat, conf] = await Promise.all([
          api.cmcResults(cmcProjectId, { limit: 1 }),
          api.cmcResults(cmcProjectId, { scope: "release", limit: 1 }),
          api.cmcResults(cmcProjectId, { scope: "stability", limit: 1 }),
          api.cmcSpecifications(cmcProjectId, { limit: 1 }),
          api.cmcBatches(cmcProjectId, { limit: 1 }),
          api.cmcConflicts(cmcProjectId),
        ]);
        if (!live) return;
        setSummary(all.summary);
        setCounts({
          results: rel.total, stability: stab.total,
          specifications: spec.total ?? 0, batches: bat.total ?? 0,
          conflicts: conf.total,
        });
        setConflicts(conf.items);
      } catch (e: any) {
        if (live) setError(e?.message ?? String(e));
      }
    })();
    return () => { live = false; };
  }, [cmcProjectId, reload]);

  // One page of the active tab.
  useEffect(() => {
    let live = true;
    setError(null);
    (async () => {
      const page = { q: query || undefined, limit: PAGE_SIZE, offset };
      try {
        if (tab === "results" || tab === "stability") {
          const res = await api.cmcResults(cmcProjectId, {
            ...page, scope: tab === "results" ? "release" : "stability" });
          if (!live) return;
          setResults(res.items);
          setPageTotal(res.total);
        } else if (tab === "specifications") {
          const spec = await api.cmcSpecifications(cmcProjectId, page);
          if (!live) return;
          setTests(spec.items);
          setPageTotal(spec.total ?? spec.items.length);
        } else if (tab === "batches") {
          const bat = await api.cmcBatches(cmcProjectId, page);
          if (!live) return;
          setBatches(bat.items);
          setPageTotal(bat.total ?? bat.items.length);
        } else {
          const conf = await api.cmcConflicts(cmcProjectId);
          if (!live) return;
          setConflicts(conf.items);
          setPageTotal(conf.total);
        }
      } catch (e: any) {
        if (live) {
          setError(e?.message ?? String(e));
          if (results === null) setResults([]);
        }
      }
    })();
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cmcProjectId, tab, offset, query, reload]);

  async function verifyAll() {
    setBusy("verify");
    try {
      const done = await api.cmcVerifyResults(cmcProjectId, { all_unverified: true });
      if (done.skipped_conflicts) {
        toast.warning(
          `${done.verified} verified. ${done.skipped_conflicts} left: two sources disagree about them.`);
      } else {
        toast.success(`${done.verified} value${done.verified === 1 ? "" : "s"} verified.`);
      }
      await load();
    } catch (e: any) {
      toast.error("Could not verify", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function resolve(row: CmcResultRow, keepId: string) {
    setBusy(row.id);
    try {
      const done = await api.cmcResolveConflict(row.id, keepId);
      toast.success(`Kept ${done.value_text}; discarded ${done.discarded}.`);
      await load();
    } catch (e: any) {
      toast.error("Could not resolve", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  function replaceRow(next: CmcResultRow) {
    const before = (results ?? []).find((r) => r.id === next.id);
    setResults((prev) => (prev ?? []).map((r) => (r.id === next.id ? { ...r, ...next } : r)));
    // Only a row that CHANGED verification state moves the counter. It used to
    // increment on every save whose row came back verified, including a row
    // that was already verified -- so re-saving one value enough times walked
    // the banner to "all verified" while unverified values sat on the server.
    // A gate that reports itself satisfied is worse than no gate.
    const gained = !before?.verified_by && !!next.verified_by;
    const lost = !!before?.verified_by && !next.verified_by;
    if (!gained && !lost) return;
    setSummary((prev) => {
      if (!prev) return prev;
      const verified = Math.max(0, Math.min(prev.total, prev.verified + (gained ? 1 : -1)));
      const unverified = Math.max(0, prev.total - verified);
      return { ...prev, verified, unverified,
               all_verified: prev.total > 0 && unverified === 0 };
    });
  }

  if (results === null) return <TableSkeleton rows={6} cols={6} />;
  if (error) return <ErrorBanner title="The data could not be loaded" message="Try again in a moment." detail={error} />;

  if (!counts.results && !counts.stability && !counts.specifications
      && !counts.batches && !query) {
    return (
      <PolishedEmpty
        icon={<ShieldQuestion className="h-8 w-8 text-muted-foreground" />}
        title="No quality data yet"
        subtitle="Upload the specification and the certificates of analysis, then process them — every value they contain appears here for checking before it can reach a document."
      />
    );
  }

  const source = (id: string | null) => (id ? sources[id] ?? "a removed source" : "no source");

  return (
    <div className="space-y-4">
      {/* The gate, stated plainly. */}
      <div className={cn(
        "flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3",
        summary?.all_verified
          ? "border-success/40 bg-success/10"
          : "border-warning/40 bg-warning/10",
      )}>
        <div className="flex items-center gap-2 text-sm">
          {summary?.all_verified
            ? <CheckCircle2 className="h-4 w-4 text-success" />
            : <AlertTriangle className="h-4 w-4 text-warning" />}
          <span className="font-medium text-foreground">
            {summary?.verified ?? 0} of {summary?.total ?? 0} values verified
          </span>
          {!!summary?.conflicts && (
            <span className="text-destructive">· {summary.conflicts} in conflict</span>
          )}
          <span className="text-muted-foreground">
            {summary?.all_verified
              ? "— these can be rendered into the dossier."
              : "— unverified values cannot reach a document."}
          </span>
        </div>
        {!summary?.all_verified && (
          <Button size="sm" onClick={verifyAll} disabled={busy !== null}>
            {busy === "verify" ? "Verifying…" : "Verify all unconflicted"}
          </Button>
        )}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex gap-1 border-b border-border">
          {TABS.map(([key, label]) => {
            const count = counts[key];
            return (
              <button
                key={key}
                onClick={() => setTab(key)}
                className={cn(
                  "inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition-colors",
                  tab === key
                    ? "border-brand text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground",
                  key === "conflicts" && count > 0 && tab !== key && "text-destructive",
                )}
              >
                {label}
                <span className="rounded-full bg-muted px-1.5 text-[0.65rem]">{count}</span>
              </button>
            );
          })}
        </div>
        <Input
          value={filter} onChange={(e) => setFilter(e.target.value)}
          placeholder="Search every test, batch or value…" className="h-8 w-64 text-xs"
        />
      </div>

      <SwapIn k={tab}>
        {(tab === "results" || tab === "stability") && (
          <div className="overflow-x-auto rounded-xl border border-border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                  <th className="px-3 py-2 font-medium">Batch</th>
                  <th className="px-3 py-2 font-medium">Test</th>
                  {tab === "stability" && <th className="px-3 py-2 font-medium">Condition</th>}
                  {tab === "stability" && <th className="px-3 py-2 font-medium">Month</th>}
                  <th className="px-3 py-2 font-medium">Value</th>
                  <th className="px-3 py-2 font-medium">Acceptance criterion</th>
                  <th className="px-3 py-2 font-medium">Conformance</th>
                  <th className="px-3 py-2 font-medium">Source</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {(results ?? []).map((row) => (
                    <tr key={row.id} className={cn(
                      "border-b border-border/60 last:border-0",
                      row.conflict_with_id && "bg-destructive/5",
                      row.conformance === "fail" && "bg-destructive/5",
                    )}>
                      <td className="px-3 py-1.5 font-mono text-xs">{row.batch_number ?? "—"}</td>
                      <td className="px-3 py-1.5">{row.test_name ?? "—"}</td>
                      {tab === "stability" && (
                        <td className="px-3 py-1.5 font-mono text-xs">{row.storage_condition ?? "—"}</td>
                      )}
                      {tab === "stability" && (
                        <td className="px-3 py-1.5 tabular-nums">{row.timepoint_months ?? "—"}</td>
                      )}
                      <td className="px-3 py-1.5">
                        <ValueCell row={row} onSaved={replaceRow} />
                      </td>
                      <td className="px-3 py-1.5 text-xs text-muted-foreground">
                        {row.acceptance_criterion_text ?? "—"}
                      </td>
                      <td className="px-3 py-1.5">
                        <ConformanceChip outcome={row.conformance} reason={row.conformance_reason} />
                      </td>
                      <td className="px-3 py-1.5 text-xs text-muted-foreground" title={
                        [source(row.source_document_id),
                         row.page ? `p.${row.page}` : null,
                         row.table_ref ? `Table ${row.table_ref}` : null]
                          .filter(Boolean).join(" · ")}>
                        <span className="block max-w-40 truncate">{source(row.source_document_id)}</span>
                      </td>
                      <td className="px-3 py-1.5">
                        <ConfidenceBadge value={row.extraction_confidence}
                                         verified={!!row.verified_by} />
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        )}

        {tab === "specifications" && (
          <div className="overflow-x-auto rounded-xl border border-border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                  <th className="px-3 py-2 font-medium">Test</th>
                  <th className="px-3 py-2 font-medium">Acceptance criterion</th>
                  <th className="px-3 py-2 font-medium">Limits</th>
                  <th className="px-3 py-2 font-medium">Method</th>
                  <th className="px-3 py-2 font-medium">Stage</th>
                  <th className="px-3 py-2 font-medium">Source</th>
                </tr>
              </thead>
              <tbody>
                {(tests ?? []).map((t) => (
                  <tr key={t.id} className="border-b border-border/60 last:border-0">
                    <td className="px-3 py-1.5 font-medium text-foreground">{t.test_name}</td>
                    <td className="px-3 py-1.5 text-xs">{t.acceptance_criterion_text ?? "—"}</td>
                    <td className="px-3 py-1.5 font-mono text-xs text-muted-foreground">
                      {t.limit_operator
                        ? `${t.limit_operator} ${[t.limit_lower, t.limit_upper].filter(Boolean).join(" – ") || "—"}`
                        : <span title="This criterion could not be reduced to a numeric bound, so conformance is judged by a person.">not comparable</span>}
                    </td>
                    <td className="px-3 py-1.5 font-mono text-xs">{t.method_id ?? "—"}</td>
                    <td className="px-3 py-1.5 text-xs capitalize">{t.stage}</td>
                    <td className="px-3 py-1.5 text-xs text-muted-foreground">
                      <span className="block max-w-40 truncate">{source(t.source_document_id)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {tab === "batches" && (
          <div className="overflow-x-auto rounded-xl border border-border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                  <th className="px-3 py-2 font-medium">Batch</th>
                  <th className="px-3 py-2 font-medium">Size</th>
                  <th className="px-3 py-2 font-medium">Manufactured</th>
                  <th className="px-3 py-2 font-medium">Purpose</th>
                  <th className="px-3 py-2 font-medium">Site</th>
                </tr>
              </thead>
              <tbody>
                {(batches ?? []).map((b) => (
                  <tr key={b.id} className="border-b border-border/60 last:border-0">
                    <td className="px-3 py-1.5 font-mono text-xs font-medium text-foreground">{b.batch_number}</td>
                    <td className="px-3 py-1.5">{[b.batch_size, b.batch_size_unit].filter(Boolean).join(" ") || "—"}</td>
                    <td className="px-3 py-1.5 text-muted-foreground">{b.manufacture_date ?? "—"}</td>
                    <td className="px-3 py-1.5 capitalize">{b.purpose ?? "—"}</td>
                    <td className="px-3 py-1.5">{b.site_name ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {tab === "conflicts" && (
          (conflicts ?? []).length === 0 ? (
            <PolishedEmpty
              icon={<CheckCircle2 className="h-8 w-8 text-success" />}
              title="No conflicts"
              subtitle="Where two sources reported the same cell, they agreed."
            />
          ) : (
            <div className="space-y-3">
              <p className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-foreground">
                <FileWarning className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                Two sources report different values for the same batch, test and timepoint.
                Nothing was merged — choose which one the dossier should carry.
              </p>
              {(conflicts ?? []).map((row) => (
                <div key={row.id}
                     className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-card px-4 py-3 text-sm">
                  <div>
                    <div className="font-medium text-foreground">
                      {row.batch_number} · {row.test_name}
                      {row.storage_condition && <span className="text-muted-foreground"> · {row.storage_condition}</span>}
                      {row.timepoint_months !== null && <span className="text-muted-foreground"> · month {row.timepoint_months}</span>}
                    </div>
                    <div className="mt-0.5 font-mono text-xs">{row.value_text}</div>
                    <div className="text-xs text-muted-foreground">from {source(row.source_document_id)}</div>
                  </div>
                  <Button size="sm" variant="outline" disabled={busy !== null}
                          onClick={() => resolve(row, row.id)}>
                    Keep this value
                  </Button>
                </div>
              ))}
            </div>
          )
        )}
      </SwapIn>

      {/* Where in the set the reader is. `total` is what the server counted,
          not what arrived -- the previous grid discarded it and truncated in
          silence, which reads exactly like a complete list. */}
      {tab !== "conflicts" && pageTotal > 0 && (
        <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
          <span>
            Showing {Math.min(offset + 1, pageTotal)}–{Math.min(offset + PAGE_SIZE, pageTotal)}
            {" "}of {pageTotal}
            {query && <span> matching “{query}”</span>}
          </span>
          {pageTotal > PAGE_SIZE && (
            <div className="flex items-center gap-1">
              <Button variant="outline" size="sm" className="h-7 px-2"
                      disabled={offset === 0}
                      onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
                Previous
              </Button>
              <span className="px-1">
                Page {Math.floor(offset / PAGE_SIZE) + 1} of {Math.ceil(pageTotal / PAGE_SIZE)}
              </span>
              <Button variant="outline" size="sm" className="h-7 px-2"
                      disabled={offset + PAGE_SIZE >= pageTotal}
                      onClick={() => setOffset(offset + PAGE_SIZE)}>
                Next
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
