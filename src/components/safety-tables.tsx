/**
 * S6: the computed tables, and the figures they divide by.
 *
 * Every number on this screen was counted by the server from the case store,
 * through the same scope layer the report is built from. Clicking a number
 * shows the cases behind it — read from the provenance recorded while counting,
 * so the drill-down cannot list a case the number does not include.
 *
 * The screen says what the tables do NOT contain as loudly as what they do. An
 * event nobody has confirmed the expectedness of sits in a column labelled "not
 * confirmed", and every gap is listed under the table. A table that closed its
 * gaps up would read as complete.
 */
import { useEffect, useState } from "react";
import {
  AlertTriangle, CheckCircle2, Info, Plus, Table2, Trash2,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type {
  PvExposure, PvReportInstance, PvTabulation, PvTabulationStatus,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorBanner } from "@/components/error-banner";
import { PolishedEmpty, TableSkeleton } from "@/components/skeletons";
import { cn } from "@/lib/utils";

const SELECT_CLASS =
  "h-8 rounded-md border border-input bg-transparent px-2 text-xs";

export function SafetyTables({ reports }: { reports: PvReportInstance[] }) {
  const [reportId, setReportId] = useState(reports.length ? reports[0].id : "");

  if (!reports.length) {
    return (
      <PolishedEmpty
        icon={<Table2 className="h-8 w-8 text-muted-foreground" />}
        title="No reporting interval yet"
        subtitle="A table counts cases within an interval's dates. Create one under Reporting intervals first."
      />
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-xs text-muted-foreground">Tables for</label>
        <select className={cn(SELECT_CLASS, "w-auto")} value={reportId}
                onChange={(e) => setReportId(e.target.value)}>
          {reports.map((r) => (
            <option key={r.id} value={r.id}>
              {r.doc_type_name} · {r.period_start} → {r.period_end} · lock {r.data_lock_point}
            </option>
          ))}
        </select>
      </div>
      {reportId && <ExposurePanel reportId={reportId} key={`exp-${reportId}`} />}
      {reportId && <TablesPanel reportId={reportId} key={`tab-${reportId}`} />}
    </div>
  );
}

function TablesPanel({ reportId }: { reportId: string }) {
  const [statuses, setStatuses] = useState<PvTabulationStatus[] | null>(null);
  const [active, setActive] = useState<string | null>(null);
  const [table, setTable] = useState<PvTabulation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [drill, setDrill] = useState<{ cell: string; label: string } | null>(null);

  useEffect(() => {
    let live = true;
    api.pvTabulations(reportId)
      .then((res) => {
        if (!live) return;
        setStatuses(res.items);
        const first = res.items.find((i) => i.available);
        setActive(first?.key ?? null);
      })
      .catch((e: any) => { if (live) { setError(e?.message ?? String(e)); setStatuses([]); } });
    return () => { live = false; };
  }, [reportId]);

  useEffect(() => {
    if (!active) { setTable(null); return; }
    let live = true;
    setTable(null);
    api.pvTabulation(reportId, active)
      .then((res) => { if (live) setTable(res); })
      .catch((e: any) => { if (live) toast.error("The table could not be built",
                                                 { description: e?.message ?? String(e) }); });
    return () => { live = false; };
  }, [reportId, active]);

  if (statuses === null) return <TableSkeleton rows={4} cols={4} />;
  if (error) {
    return <ErrorBanner title="The tables could not be listed"
                        message="Try again in a moment." detail={error} />;
  }
  if (!statuses.length) {
    return <p className="text-sm text-muted-foreground">
      This report type declares no computed tables.
    </p>;
  }

  return (
    <section className="space-y-3">
      <h3 className="font-medium">Computed tables</h3>
      <div className="flex flex-wrap gap-1.5">
        {statuses.map((status) => (
          <button key={status.key} onClick={() => status.available && setActive(status.key)}
                  title={status.reason ?? undefined}
                  className={cn(
                    "rounded-full border px-3 py-1 text-xs transition-colors",
                    active === status.key ? "border-brand bg-brand/10 text-brand"
                      : status.available ? "border-border hover:bg-accent"
                      : "cursor-not-allowed border-dashed border-border text-muted-foreground")}>
            {status.key.replace(/_/g, " ")}
            {status.available && status.missing > 0 && (
              <span className="ml-1.5 text-warning">· {status.missing} gap(s)</span>
            )}
            {!status.available && <span className="ml-1.5">· no data</span>}
          </button>
        ))}
      </div>

      {active && !table && <TableSkeleton rows={5} cols={6} />}
      {table && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h4 className="text-sm font-medium">{table.title}</h4>
            <span className="font-mono text-[0.65rem] text-muted-foreground">
              {table.scope.period_start} → {table.scope.period_end} · lock{" "}
              {table.scope.data_lock_point}
              {table.scope.cumulative_from &&
                ` · cumulative from ${table.scope.cumulative_from} (${String(table.scope.cumulative_anchor).toUpperCase()})`}
            </span>
          </div>
          <div className="overflow-x-auto rounded-xl border border-border">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border bg-muted/40 text-left text-muted-foreground">
                  {table.columns.map((column) => (
                    <th key={column} className="px-2 py-1.5 font-medium">{column}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {table.rows.map((row, r) => (
                  <tr key={r} className="border-b border-border/60 last:border-0">
                    {row.map((value, c) => {
                      const cellId = `r${r}c${c}`;
                      const traced = table.cells[cellId];
                      const clickable = traced && traced.events.length > 0;
                      return (
                        <td key={c} className="px-2 py-1.5">
                          {clickable ? (
                            <button onClick={() => setDrill({
                                        cell: cellId,
                                        label: `${row[1] ?? row[0]} · ${table.columns[c]}` })}
                                    className="font-medium text-brand underline-offset-2 hover:underline">
                              {value}
                            </button>
                          ) : (
                            <span className={value === "0" ? "text-muted-foreground" : ""}>
                              {value}
                            </span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {(table.missing.length > 0 || table.notes.length > 0) && (
            <div className="space-y-1 rounded-lg border border-border bg-muted/20 px-3 py-2">
              {table.missing.map((gap, i) => (
                <p key={`m${i}`} className="flex items-start gap-1.5 text-xs text-warning">
                  <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" /> {gap}
                </p>
              ))}
              {table.notes.map((note, i) => (
                <p key={`n${i}`} className="flex items-start gap-1.5 text-xs text-muted-foreground">
                  <Info className="mt-0.5 h-3 w-3 shrink-0" /> {note}
                </p>
              ))}
            </div>
          )}
        </div>
      )}

      {drill && table && (
        <Drilldown reportId={reportId} tableKey={table.key} cell={drill.cell}
                   label={drill.label} onClose={() => setDrill(null)} />
      )}
    </section>
  );
}

function Drilldown({ reportId, tableKey, cell, label, onClose }: {
  reportId: string; tableKey: string; cell: string; label: string; onClose: () => void;
}) {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.pvDrilldown>> | null>(null);

  useEffect(() => {
    let live = true;
    api.pvDrilldown(reportId, tableKey, cell)
      .then((res) => { if (live) setData(res); })
      .catch((e: any) => toast.error("The cases could not be loaded",
                                     { description: e?.message ?? String(e) }));
    return () => { live = false; };
  }, [reportId, tableKey, cell]);

  return (
    <div className="rounded-xl border border-brand/40 bg-brand/5 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h4 className="text-sm font-medium">{label}</h4>
          <p className="text-xs text-muted-foreground">
            The cases counted in this cell, exactly as they were counted.
          </p>
        </div>
        <Button size="sm" variant="outline" onClick={onClose}>Close</Button>
      </div>
      {!data ? <div className="mt-3"><TableSkeleton rows={3} cols={4} /></div> : (
        <table className="mt-3 w-full text-xs">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1 font-medium">Case</th>
              <th className="py-1 font-medium">Country</th>
              <th className="py-1 font-medium">Received</th>
              <th className="py-1 font-medium">Serious</th>
            </tr>
          </thead>
          <tbody>
            {data.cases.map((c) => (
              <tr key={c.id} className="border-t border-border/60">
                <td className="py-1 font-mono">{c.worldwide_case_id ?? c.id}</td>
                <td className="py-1">{c.country_of_occurrence ?? "—"}</td>
                <td className="py-1 font-mono">{c.initial_receipt_date ?? "—"}</td>
                <td className="py-1">{c.is_serious ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ExposurePanel({ reportId }: { reportId: string }) {
  const [rows, setRows] = useState<PvExposure[] | null>(null);
  const [contexts, setContexts] = useState<string[]>([]);
  const [measures, setMeasures] = useState<string[]>([]);
  const [form, setForm] = useState({
    context: "clinical_trial", measure: "subjects", value_text: "", region: "",
    calculation_method_note: "",
  });
  const [busy, setBusy] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let live = true;
    api.pvExposure(reportId)
      .then((res) => {
        if (!live) return;
        setRows(res.items);
        setContexts(res.contexts);
        setMeasures(res.measures);
      })
      .catch((e: any) => toast.error("Exposure could not be loaded",
                                     { description: e?.message ?? String(e) }));
    return () => { live = false; };
  }, [reportId, reload]);

  async function add() {
    if (!form.value_text.trim()) {
      toast.error("An exposure figure needs a value.");
      return;
    }
    setBusy("add");
    try {
      await api.pvAddExposure(reportId, {
        context: form.context, measure: form.measure,
        value_text: form.value_text.trim(),
        region: form.region.trim() || undefined,
        calculation_method_note: form.calculation_method_note.trim() || undefined,
      });
      setForm((f) => ({ ...f, value_text: "", region: "", calculation_method_note: "" }));
      setReload((n) => n + 1);
    } catch (e: any) {
      toast.error("The figure could not be added", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function act(row: PvExposure, action: "confirm" | "delete") {
    setBusy(row.id);
    try {
      if (action === "confirm") await api.pvConfirmExposure(row.id);
      else await api.pvDeleteExposure(row.id);
      setReload((n) => n + 1);
    } catch (e: any) {
      toast.error(action === "confirm" ? "The figure could not be confirmed"
                                       : "The figure could not be deleted",
                  { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="space-y-3 rounded-xl border border-border p-4">
      <div>
        <h3 className="font-medium">Exposure</h3>
        <p className="text-xs text-muted-foreground">
          The denominator of every rate in the report. Kept as stated — “1,240,000” and
          “1.24 million” are different claims about precision — and confirmed by a
          reviewer, with the method that produced it.
        </p>
      </div>

      {rows === null ? <TableSkeleton rows={2} cols={5} /> : rows.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border bg-muted/40 text-left text-muted-foreground">
                <th className="px-2 py-1.5 font-medium">Context</th>
                <th className="px-2 py-1.5 font-medium">Region</th>
                <th className="px-2 py-1.5 font-medium">Measure</th>
                <th className="px-2 py-1.5 font-medium">Value</th>
                <th className="px-2 py-1.5 font-medium">Method</th>
                <th className="px-2 py-1.5 font-medium">State</th>
                <th className="px-2 py-1.5" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className="border-b border-border/60 last:border-0">
                  <td className="px-2 py-1.5">{row.context.replace("_", " ")}</td>
                  <td className="px-2 py-1.5">{row.region ?? "—"}</td>
                  <td className="px-2 py-1.5">{row.measure.replace(/_/g, " ")}</td>
                  <td className="px-2 py-1.5 font-mono">{row.value_text}</td>
                  <td className="max-w-[16rem] truncate px-2 py-1.5 text-muted-foreground">
                    {row.calculation_method_note ?? <span className="text-warning">no method</span>}
                  </td>
                  <td className="px-2 py-1.5">
                    {row.confirmed_by
                      ? <span className="inline-flex items-center gap-1 text-success">
                          <CheckCircle2 className="h-3 w-3" /> confirmed
                        </span>
                      : <span className="text-warning">unconfirmed</span>}
                  </td>
                  <td className="px-2 py-1.5 text-right">
                    <div className="flex justify-end gap-1">
                      {!row.confirmed_by && (
                        <Button size="sm" variant="outline" className="h-6 px-2 text-[0.65rem]"
                                disabled={busy !== null} onClick={() => act(row, "confirm")}>
                          Confirm
                        </Button>
                      )}
                      <button onClick={() => act(row, "delete")} disabled={busy !== null}
                              className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive">
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="grid gap-2 sm:grid-cols-[9rem_9rem_8rem_6rem_1fr_auto]">
        <select className={SELECT_CLASS} value={form.context}
                onChange={(e) => setForm((f) => ({ ...f, context: e.target.value }))}>
          {contexts.map((c) => <option key={c} value={c}>{c.replace("_", " ")}</option>)}
        </select>
        <select className={SELECT_CLASS} value={form.measure}
                onChange={(e) => setForm((f) => ({ ...f, measure: e.target.value }))}>
          {measures.map((m) => <option key={m} value={m}>{m.replace(/_/g, " ")}</option>)}
        </select>
        <Input className="h-8 text-xs" placeholder="Value, e.g. 1,240" value={form.value_text}
               onChange={(e) => setForm((f) => ({ ...f, value_text: e.target.value }))} />
        <Input className="h-8 text-xs" placeholder="Region" value={form.region}
               onChange={(e) => setForm((f) => ({ ...f, region: e.target.value }))} />
        <Input className="h-8 text-xs" placeholder="How it was calculated"
               value={form.calculation_method_note}
               onChange={(e) => setForm((f) => ({ ...f, calculation_method_note: e.target.value }))} />
        <Button size="sm" className="h-8" onClick={add} disabled={busy !== null}>
          <Plus className="mr-1 h-3.5 w-3.5" /> Add
        </Button>
      </div>
    </section>
  );
}
