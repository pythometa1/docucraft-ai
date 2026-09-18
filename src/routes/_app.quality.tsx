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
import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Info, Loader2, ShieldCheck, TrendingDown, TrendingUp } from "lucide-react";

import { api, ApiError } from "@/lib/api";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { ErrorBanner } from "@/components/error-banner";
import { FadeIn, Stagger, StaggerItem, useCountUp } from "@/components/motion";
import { plainly } from "@/components/processing-banner";
import { SkeletonBar, TableSkeleton } from "@/components/skeletons";
import { cn } from "@/lib/utils";
import type { QualityMetric, QualityReport, SloRow } from "@/lib/types";

/**
 * Server prose on its way to the screen.
 *
 * Everything on this page is written by `metrics.py`, and §22's own wording is
 * the engine's: it names the intermediate structure the compiler builds and
 * talks about compilation. `plainly()` is the shared scrub and does most of the
 * work; the two extra forms here are the noun and the past tense, which §18's
 * operation names use and the stage labels `plainly()` was written for never
 * produce.
 *
 * Rewording `metrics.py` was the alternative and it is the wrong fix: those same
 * strings are read by the audit trail and by the tests, so renaming them for the
 * benefit of a heading would change a record somebody may later have to defend.
 */
function readable(text: string): string {
  return plainly(text)
    .replace(/\bcompilations?\b/gi, "processing")
    .replace(/\bcompiled\b/gi, "processed");
}

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
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    api.qualityReport(windowDays)
      .then((r) => { setReport(r); setDenied(false); setError(null); })
      .catch((e: any) => {
        // 403 is not an error to shout about: the role simply does not read the
        // audit trail, and these numbers are gated with it.
        if (e instanceof ApiError && e.status === 403) { setDenied(true); return; }
        setError(e?.message ?? String(e));
      })
      .finally(() => setLoading(false));
  }, [windowDays]);

  useEffect(() => { load(); }, [load]);

  if (denied) {
    return (
      <FadeIn className="p-6 lg:p-8 max-w-3xl mx-auto">
        <div className="rounded-xl surface-raised p-8 text-center space-y-2">
          <ShieldCheck className="h-8 w-8 mx-auto text-muted-foreground" />
          <h1 className="text-lg font-semibold">Not your numbers to read</h1>
          <p className="text-sm text-muted-foreground max-w-md mx-auto">
            These are a summary of the whole estate — its error rate and its review behaviour — so
            they are gated on the same permission as the audit trail. Ask an administrator if you
            need them.
          </p>
        </div>
      </FadeIn>
    );
  }

  return (
    <TooltipProvider delayDuration={150}>
      <div className="p-6 lg:p-8 max-w-7xl mx-auto space-y-6">
        <FadeIn className="flex items-start justify-between flex-wrap gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-gradient">Quality</h1>
            <p className="text-sm text-muted-foreground mt-1 max-w-2xl">
              How often this system is wrong, how often a person has to step in, and how long it
              takes. Measured from what actually ran — a metric with nothing behind it says so
              rather than reporting zero.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {/* Re-measuring keeps the previous window's numbers on screen under
                this spinner. Blanking them would read as the figures having gone
                away rather than as a slower query. */}
            {loading && report && (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin text-muted-foreground" />
            )}
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
        </FadeIn>

        {error && (
          <ErrorBanner
            title="Could not load the quality report"
            message={report
              ? "The figures below are from the last window that loaded."
              : "Nothing was measured wrongly — the report simply could not be fetched."}
            detail={error}
            onRetry={load}
            retrying={loading}
          />
        )}

        {/* Placeholders in the real grid, at the real row heights. These numbers
            take a couple of seconds to compute over the audit trail, and a
            spinner that is then replaced by nine cards and a seven-column table
            moves the whole page under the reader's cursor. */}
        {loading && !report && <QualitySkeleton />}

        {report && (
          <>
            <p className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
              {readable(report.ordering_note)}
            </p>

            <Stagger className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {report.metrics.map((m) => (
                <StaggerItem key={m.key} className="h-full">
                  <MetricCard metric={m} />
                </StaggerItem>
              ))}
            </Stagger>

            <SloTable slos={report.slos} />

            <Calibration calibration={report.calibration} />
          </>
        )}
      </div>
    </TooltipProvider>
  );
}

/** The page at the size it will be: the ordering note, nine metric cards, and the
 *  timing table with its real header above placeholder rows. */
function QualitySkeleton() {
  return (
    <>
      <SkeletonBar className="h-12 w-full rounded-lg" />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {Array.from({ length: 9 }).map((_, i) => (
          <div key={i} className="rounded-xl border border-border bg-card p-4">
            <SkeletonBar className="h-2.5 w-28" />
            <SkeletonBar className="mt-3.5 h-7 w-24" />
            <SkeletonBar className="mt-2 h-2 w-16" />
          </div>
        ))}
      </div>

      <div className="rounded-xl surface-raised overflow-hidden">
        <div className="space-y-2 border-b border-border px-4 py-3">
          <SkeletonBar className="h-3 w-44" />
          <SkeletonBar className="h-2.5 w-2/3" />
        </div>
        <table className="w-full text-sm">
          <TableSkeleton rows={5} cols={7} />
        </table>
      </div>
    </>
  );
}

function MetricCard({ metric }: { metric: QualityMetric }) {
  const Direction = metric.direction.startsWith("Down") ? TrendingDown : TrendingUp;

  // Counting up is switched off when there is no measurement, so a metric whose
  // whole point is that nobody has measured it can never animate through zero on
  // its way to a dash. The rounding keeps the climb at the precision the server
  // sent: an escaped-error rate of 0.4% must not read 0.3719 on the way there.
  const decimals = (String(metric.value ?? "").split(".")[1] ?? "").length;
  const counted = useCountUp(metric.value ?? 0, 700, metric.value != null);
  const live = Number(counted.toFixed(decimals));

  const shown = metric.value == null
    ? "—"
    : metric.unit === "%" || metric.unit.includes("percent")
      ? `${live}%`
      : String(live);

  return (
    <div className={cn(
      "h-full rounded-xl border bg-card p-4",
      // The one §22 calls the real metric, marked as such rather than left to
      // look like the other eight.
      metric.rank === 1 ? "border-brand/50" : "border-border",
    )}>
      <div className="flex items-start justify-between gap-2">
        <span className="text-xs uppercase tracking-wider text-muted-foreground">
          {readable(metric.label)}
        </span>
        <Tooltip>
          <TooltipTrigger asChild>
            <button className="text-muted-foreground/60 transition-colors hover:text-foreground">
              <Info className="h-3.5 w-3.5" />
            </button>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs text-xs space-y-1">
            <p>{readable(metric.definition)}</p>
            <p className="text-muted-foreground">Wanted: {readable(metric.direction).toLowerCase()}</p>
          </TooltipContent>
        </Tooltip>
      </div>

      <div className={cn("mt-2 flex items-baseline gap-2 text-2xl font-semibold tabular-nums",
                         !metric.available && "text-muted-foreground/50")}>
        {shown}
        {metric.available && <Direction className="h-4 w-4 text-muted-foreground" />}
      </div>
      <div className="mt-0.5 text-[11px] text-muted-foreground">{readable(metric.unit)}</div>

      {!metric.available && (
        // Not a caveat under a number: it replaces the number.
        <p className="mt-2 text-[11px] leading-snug text-muted-foreground">
          {readable(metric.unavailable_reason ?? "")}
        </p>
      )}
      {metric.available && metric.note && (
        <p className="mt-2 text-[11px] leading-snug text-muted-foreground">{readable(metric.note)}</p>
      )}
    </div>
  );
}

/** The same three words the rest of the app uses for sure, stopped and idle, so
 *  a breaching target and a blocked document are not two different reds. */
const SLO_TONE: Record<string, string> = {
  meeting: "text-ai-confident bg-ai-confident/10 border-ai-confident/30",
  breaching: "text-ai-blocked bg-ai-blocked/10 border-ai-blocked/30",
  unmeasured: "text-ai-idle bg-ai-idle/10 border-ai-idle/30",
};

function SloTable({ slos }: { slos: QualityReport["slos"] }) {
  return (
    <div className="rounded-xl surface-raised overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h2 className="text-sm font-semibold">How long things take</h2>
        <p className="mt-1 flex items-start gap-1.5 text-xs text-muted-foreground">
          <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0 text-ai-uncertain" />
          {readable(slos.caveat)}
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
        <div className="font-medium">{readable(row.dimension)}</div>
        {/* The operation key is left exactly as the server names it. It is the
            one string on this page meant to be quoted back in a support thread,
            and a prettied-up identifier cannot be looked up. */}
        <code className="text-[11px] font-mono text-muted-foreground">{row.operation}</code>
      </td>
      <td className="px-4 py-2 text-muted-foreground">
        <Tooltip>
          <TooltipTrigger className="text-left underline decoration-dotted underline-offset-2">
            {readable(row.target)}
          </TooltipTrigger>
          <TooltipContent className="max-w-xs text-xs">{readable(row.rationale)}</TooltipContent>
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
          <span className="text-ai-blocked"> · {row.failed_runs} failed</span>
        )}
      </td>
      <td className="px-4 py-2">
        <span
          title={row.reason ? readable(row.reason) : undefined}
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
  const logged = Math.round(useCountUp(calibration.suggestions_logged));

  return (
    <div className="rounded-xl surface-raised p-4 space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold">Confidence calibration</h2>
        <span className={cn(
          "rounded-full border px-2 py-0.5 text-[11px] font-medium",
          calibration.weights_calibrated
            ? "text-ai-confident bg-ai-confident/10 border-ai-confident/30"
            : "text-ai-uncertain bg-ai-uncertain/10 border-ai-uncertain/30",
        )}>
          {calibration.weights_calibrated ? "fitted" : "starting values"}
        </span>
      </div>
      <p className="text-xs text-muted-foreground">{readable(calibration.note)}</p>

      <div className="text-2xl font-semibold tabular-nums">
        {logged.toLocaleString()}
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
