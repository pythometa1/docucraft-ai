/**
 * §22's metrics and §18's targets, on a screen.
 *
 * `backend/app/metrics.py` is 1,458 lines, it has been populated by the live
 * compile, parse and render paths since it was written, and
 * `grep -rn "metrics" src/` returned nothing at all. The two numbers §22 says
 * every roadmap decision should be weighed against -- escaped error rate first,
 * automation second -- were measured continuously and shown to nobody.
 *
 * Three things the backend is careful about, that this screen has to be equally
 * careful about or the care is wasted:
 *
 * **A metric with no data is a dash, never a zero.** A 0.0 escaped-error rate on
 * an org with no way to report a defect is the most flattering wrong number this
 * system could print.
 *
 * **A target is not a measurement.** §18 opens by warning that none of its
 * figures is a benchmark result. `target` and `measured` are separate fields
 * server-side and they stay separate columns here; an untimed row says
 * "not measured", not "meeting".
 *
 * **The order is part of the content.** §22: "Auto-map rate is the vanity
 * metric. Escaped error rate is the real one." The server sends them ranked and
 * this renders them in the order it was given.
 */

import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { AlertTriangle, Info, Loader2, ShieldCheck, TrendingDown, TrendingUp } from "lucide-react";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { QualityMetric, QualityReport, SloRow } from "@/lib/types";

export const Route = createFileRoute("/_app/quality")({
  head: () => ({
    meta: [
      { title: "Quality — DocuMind AI" },
      { name: "description", content: "Escaped error rate, QA block rate, human-touch rate and the timing targets, measured rather than assumed." },
    ],
  }),
  component: QualityPage,
});

const WINDOWS = [7, 30, 90];

function QualityPage() {
  const [windowDays, setWindowDays] = useState(30);
  const [report, setReport] = useState<QualityReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [denied, setDenied] = useState(false);

  useEffect(() => {
    setLoading(true);
    api.qualityReport(windowDays)
      .then((r) => { setReport(r); setDenied(false); })
      .catch((e: any) => {
        // 403 is not an error to shout about: the role simply does not read the
        // audit trail, and these numbers are gated with it.
        if (e instanceof ApiError && e.status === 403) { setDenied(true); return; }
        toast.error("Could not load the quality report", { description: e?.message ?? String(e) });
      })
      .finally(() => setLoading(false));
  }, [windowDays]);

  if (denied) {
    return (
      <div className="p-6 lg:p-8 max-w-3xl mx-auto">
        <div className="rounded-xl surface-raised p-8 text-center space-y-2">
          <ShieldCheck className="h-8 w-8 mx-auto text-muted-foreground" />
          <h1 className="text-lg font-semibold">Not your numbers to read</h1>
          <p className="text-sm text-muted-foreground max-w-md mx-auto">
            These are a summary of the whole estate — its error rate and its review behaviour — so
            they are gated on the same permission as the audit trail. Ask an administrator if you
            need them.
          </p>
        </div>
      </div>
    );
  }

  return (
    <TooltipProvider delayDuration={150}>
      <div className="p-6 lg:p-8 max-w-7xl mx-auto space-y-6">
        <div className="flex items-start justify-between flex-wrap gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-gradient">Quality</h1>
            <p className="text-sm text-muted-foreground mt-1 max-w-2xl">
              How often this system is wrong, how often a person has to step in, and how long it
              takes. Measured from what actually ran — a metric with nothing behind it says so
              rather than reporting zero.
            </p>
          </div>
          <div className="flex gap-2">
            {WINDOWS.map((w) => (
              <button
                key={w}
                onClick={() => setWindowDays(w)}
                className={cn(
                  "px-3 py-1.5 rounded-md text-xs font-medium border",
                  windowDays === w
                    ? "bg-primary text-primary-foreground border-primary"
                    : "border-border hover:bg-muted",
                )}
              >
                {w} days
              </button>
            ))}
          </div>
        </div>

        {loading && (
          <div className="rounded-xl surface-raised p-10 text-sm text-muted-foreground flex items-center justify-center gap-2">
            <Loader2 className="h-4 w-4 animate-spin" /> Measuring…
          </div>
        )}

        {!loading && report && (
          <>
            <p className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
              {report.ordering_note}
            </p>

            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {report.metrics.map((m) => <MetricCard key={m.key} metric={m} />)}
            </div>

            <SloTable slos={report.slos} />

            <Calibration calibration={report.calibration} />
          </>
        )}
      </div>
    </TooltipProvider>
  );
}

function MetricCard({ metric }: { metric: QualityMetric }) {
  const Direction = metric.direction.startsWith("Down") ? TrendingDown : TrendingUp;
  const shown = metric.value == null
    ? "—"
    : metric.unit === "%" || metric.unit.includes("percent")
      ? `${metric.value}%`
      : String(metric.value);

  return (
    <div className={cn(
      "rounded-xl border bg-card p-4",
      // The one §22 calls the real metric, marked as such rather than left to
      // look like the other eight.
      metric.rank === 1 ? "border-brand/50" : "border-border",
    )}>
      <div className="flex items-start justify-between gap-2">
        <span className="text-xs uppercase tracking-wider text-muted-foreground">
          {metric.label}
        </span>
        <Tooltip>
          <TooltipTrigger asChild>
            <button className="text-muted-foreground/60 transition-colors hover:text-foreground">
              <Info className="h-3.5 w-3.5" />
            </button>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs text-xs space-y-1">
            <p>{metric.definition}</p>
            <p className="text-muted-foreground">Wanted: {metric.direction.toLowerCase()}</p>
          </TooltipContent>
        </Tooltip>
      </div>

      <div className={cn("mt-2 flex items-baseline gap-2 text-2xl font-semibold tabular-nums",
                         !metric.available && "text-muted-foreground/50")}>
        {shown}
        {metric.available && <Direction className="h-4 w-4 text-muted-foreground" />}
      </div>
      <div className="mt-0.5 text-[11px] text-muted-foreground">{metric.unit}</div>

      {!metric.available && (
        // Not a caveat under a number: it replaces the number.
        <p className="mt-2 text-[11px] leading-snug text-muted-foreground">
          {metric.unavailable_reason}
        </p>
      )}
      {metric.available && metric.note && (
        <p className="mt-2 text-[11px] leading-snug text-muted-foreground">{metric.note}</p>
      )}
    </div>
  );
}

const SLO_TONE: Record<string, string> = {
  meeting: "text-success bg-success/10 border-success/30",
  breaching: "text-destructive bg-destructive/10 border-destructive/30",
  unmeasured: "text-muted-foreground bg-muted border-border",
};

function SloTable({ slos }: { slos: QualityReport["slos"] }) {
  return (
    <div className="rounded-xl surface-raised overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h2 className="text-sm font-semibold">How long things take</h2>
        <p className="mt-1 flex items-start gap-1.5 text-xs text-muted-foreground">
          <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
          {slos.caveat}
        </p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-xs uppercase tracking-wider text-muted-foreground">
            <tr className="border-b border-border">
              <th className="px-4 py-2 text-left font-medium">Operation</th>
              <th className="px-4 py-2 text-left font-medium">Target</th>
              <th className="px-4 py-2 text-right font-medium">Measured</th>
              <th className="px-4 py-2 text-right font-medium">p50</th>
              <th className="px-4 py-2 text-right font-medium">Worst</th>
              <th className="px-4 py-2 text-right font-medium">Runs</th>
              <th className="px-4 py-2 text-left font-medium">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {slos.operations.map((row) => <SloRowView key={row.operation} row={row} />)}
          </tbody>
        </table>
      </div>
      <div className="px-4 py-2 border-t border-border text-xs text-muted-foreground">
        {slos.measured_operations} of {slos.operations.length} operations have been timed in the
        last {slos.window_days} days.
      </div>
    </div>
  );
}

function ms(value: number | null | undefined): string {
  if (value == null) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
}

function SloRowView({ row }: { row: SloRow }) {
  return (
    <tr className="hover:bg-accent/30">
      <td className="px-4 py-2">
        <div className="font-medium">{row.dimension}</div>
        <code className="text-[11px] font-mono text-muted-foreground">{row.operation}</code>
      </td>
      <td className="px-4 py-2 text-muted-foreground">
        <Tooltip>
          <TooltipTrigger className="text-left underline decoration-dotted underline-offset-2">
            {row.target}
          </TooltipTrigger>
          <TooltipContent className="max-w-xs text-xs">{row.rationale}</TooltipContent>
        </Tooltip>
      </td>
      {/* Measurement and target never share a cell. An untimed row shows a dash
          here rather than repeating the target as though it had been observed. */}
      <td className="px-4 py-2 text-right tabular-nums">{ms(row.measured?.headline_ms)}</td>
      <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">{ms(row.measured?.p50_ms)}</td>
      <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">{ms(row.measured?.worst_ms)}</td>
      <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">
        {row.measured?.samples ?? 0}
        {row.failed_runs > 0 && (
          <span className="text-destructive"> · {row.failed_runs} failed</span>
        )}
      </td>
      <td className="px-4 py-2">
        <span
          title={row.reason}
          className={cn(
            "inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium",
            SLO_TONE[row.status],
          )}
        >
          {row.status === "unmeasured" ? "not measured" : row.status}
        </span>
      </td>
    </tr>
  );
}

function Calibration({ calibration }: { calibration: QualityReport["calibration"] }) {
  const decisions = Object.entries(calibration.by_decision);
  const bands = Object.entries(calibration.by_band);

  return (
    <div className="rounded-xl surface-raised p-4 space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold">Confidence calibration</h2>
        <span className={cn(
          "rounded-full border px-2 py-0.5 text-[11px] font-medium",
          calibration.weights_calibrated
            ? "text-success bg-success/10 border-success/30"
            : "text-warning bg-warning/10 border-warning/30",
        )}>
          {calibration.weights_calibrated ? "fitted" : "starting values"}
        </span>
      </div>
      <p className="text-xs text-muted-foreground">{calibration.note}</p>

      <div className="text-2xl font-semibold tabular-nums">
        {calibration.suggestions_logged.toLocaleString()}
        <span className="ml-2 text-xs font-normal text-muted-foreground">
          reviewer decisions logged
        </span>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <div className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">
            By decision
          </div>
          <div className="rounded-lg border border-border divide-y divide-border text-sm">
            {decisions.map(([key, count]) => (
              <div key={key} className="flex items-center justify-between px-3 py-1.5">
                <span className="capitalize">{key.replace(/_/g, " ")}</span>
                <span className="tabular-nums text-muted-foreground">{count}</span>
              </div>
            ))}
          </div>
        </div>
        <div>
          <div className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">
            By confidence band
          </div>
          <div className="rounded-lg border border-border divide-y divide-border text-sm">
            {bands.length === 0 && (
              <div className="px-3 py-1.5 text-muted-foreground">Nothing logged yet.</div>
            )}
            {bands.map(([band, count]) => (
              <div key={band} className="flex items-center justify-between px-3 py-1.5">
                <code className="text-xs font-mono">{band}</code>
                <span className="tabular-nums text-muted-foreground">{count}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
