/**
 * S9: everything that stands between a periodic safety report and export.
 *
 * The verdict at the top is not a preview of the export gate — it IS the gate.
 * The server computes "exportable" from exactly the list below, with the same
 * function the export endpoint calls, so this screen and the export cannot
 * disagree. Approval and sign-off are findings here for the same reason.
 *
 * Every finding says what is wrong and where. A check that could not run is
 * shown as a blocker naming the check: it has not passed.
 */
import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle, CheckCircle2, Info, OctagonX, RefreshCw, ShieldCheck,
} from "lucide-react";

import { api } from "@/lib/api";
import type { PvFinding, PvQcReport, PvReportInstance } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { ErrorBanner } from "@/components/error-banner";
import { PolishedEmpty, TableSkeleton } from "@/components/skeletons";
import { cn } from "@/lib/utils";

const SELECT_CLASS =
  "h-8 rounded-md border border-input bg-transparent px-2 text-xs";

export function SafetyQc({ reports, children }: {
  reports: PvReportInstance[];
  /** The export and sign-off panel, rendered under the findings for the
   *  selected report. */
  children?: (report: PvReportInstance, qc: PvQcReport | null,
              refresh: () => void) => React.ReactNode;
}) {
  const [reportId, setReportId] = useState(reports.length ? reports[0].id : "");
  const report = reports.find((r) => r.id === reportId) ?? null;

  if (!reports.length) {
    return (
      <PolishedEmpty
        icon={<ShieldCheck className="h-8 w-8 text-muted-foreground" />}
        title="No reporting interval yet"
        subtitle="Quality checks run against one report. Create one under Reporting intervals first."
      />
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-xs text-muted-foreground">Checks for</label>
        <select className={cn(SELECT_CLASS, "w-auto max-w-full")} value={reportId}
                onChange={(e) => setReportId(e.target.value)}>
          {reports.map((r) => (
            <option key={r.id} value={r.id}>
              {r.doc_type_name} · {r.period_start} → {r.period_end} · lock {r.data_lock_point}
            </option>
          ))}
        </select>
      </div>
      {report && <QcPanel report={report} key={report.id}>{children}</QcPanel>}
    </div>
  );
}

function QcPanel({ report, children }: {
  report: PvReportInstance;
  children?: (report: PvReportInstance, qc: PvQcReport | null,
              refresh: () => void) => React.ReactNode;
}) {
  const [qc, setQc] = useState<PvQcReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick((n) => n + 1), []);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    api.pvQc(report.id)
      .then((res) => { if (live) setQc(res); })
      .catch((e: any) => { if (live) setError(e?.message ?? String(e)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [report.id, tick]);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Verdict qc={qc} loading={loading} />
        <Button size="sm" variant="outline" onClick={refresh} disabled={loading}>
          <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", loading && "animate-spin")} />
          Run checks again
        </Button>
      </div>
      {error && <ErrorBanner title="The checks could not run" message={error} onRetry={refresh} />}
      {loading && !qc && <TableSkeleton rows={6} />}
      {qc && (
        <div className="space-y-4">
          <FindingGroup title="Blockers" hint="Each one stops export."
                        findings={qc.blockers} tone="blocker" />
          <FindingGroup title="Warnings" hint="Recorded with the export. They do not stop it."
                        findings={qc.warnings} tone="warning" />
          <FindingGroup title="Information" hint="Worth knowing before sign-off."
                        findings={qc.info} tone="info" />
        </div>
      )}
      {children?.(report, qc, refresh)}
    </div>
  );
}

function Verdict({ qc, loading }: { qc: PvQcReport | null; loading: boolean }) {
  if (!qc) {
    return <p className="text-sm text-muted-foreground">{loading ? "Running checks…" : ""}</p>;
  }
  if (qc.exportable) {
    return (
      <div className="flex items-center gap-2 text-sm font-medium text-emerald-700 dark:text-emerald-400">
        <CheckCircle2 className="h-4 w-4" />
        No blockers. This report can be exported.
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2 text-sm font-medium text-destructive">
      <OctagonX className="h-4 w-4" />
      {qc.blockers.length} blocker{qc.blockers.length === 1 ? "" : "s"}. Export is refused until
      each is resolved.
    </div>
  );
}

const TONE = {
  blocker: { icon: OctagonX, text: "text-destructive", ring: "border-destructive/30" },
  warning: { icon: AlertTriangle, text: "text-amber-700 dark:text-amber-400",
             ring: "border-amber-500/30" },
  info: { icon: Info, text: "text-muted-foreground", ring: "border-border" },
} as const;

function FindingGroup({ title, hint, findings, tone }: {
  title: string; hint: string; findings: PvFinding[]; tone: keyof typeof TONE;
}) {
  const style = TONE[tone];
  const Icon = style.icon;
  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className={cn("text-sm font-semibold", findings.length && style.text)}>
          {title} <span className="font-normal text-muted-foreground">({findings.length})</span>
        </h3>
        <span className="text-xs text-muted-foreground">{hint}</span>
      </div>
      {findings.length === 0 ? (
        <p className="text-xs text-muted-foreground">None.</p>
      ) : (
        <ul className="space-y-1.5">
          {findings.map((f, i) => (
            <li key={`${f.code}-${f.section_code ?? ""}-${i}`}
                className={cn("rounded-md border px-3 py-2 text-sm", style.ring)}>
              <div className="flex items-start gap-2">
                <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", style.text)} />
                <div className="min-w-0 space-y-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <code className="rounded bg-muted px-1.5 py-0.5 text-[11px]">{f.code}</code>
                    {f.section_code && (
                      <span className="rounded border px-1.5 py-0.5 text-[11px] text-muted-foreground">
                        § {f.section_code}
                      </span>
                    )}
                  </div>
                  <p className="break-words">{f.message}</p>
                  <Evidence detail={f.detail} />
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** The ids and figures behind a finding, shown plainly: what the check saw. */
function Evidence({ detail }: { detail: Record<string, unknown> }) {
  const entries = Object.entries(detail ?? {}).filter(([, v]) => v !== null && v !== undefined);
  if (!entries.length) return null;
  return (
    <dl className="flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-muted-foreground">
      {entries.map(([k, v]) => (
        <div key={k} className="flex min-w-0 gap-1">
          <dt>{k.replace(/_/g, " ")}:</dt>
          <dd className="truncate font-mono" title={render(v)}>{render(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

function render(v: unknown): string {
  if (Array.isArray(v)) {
    const shown = v.slice(0, 5).map(String).join(", ");
    return v.length > 5 ? `${shown} … (+${v.length - 5})` : shown;
  }
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
