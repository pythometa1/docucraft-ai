/**
 * What the estate produced, how long it took, and what it cost.
 *
 * The page this replaces rendered real queries over instrumentation nothing
 * wrote to. Its token tile could only ever show 0, because the column it summed
 * was never written by anything; its four range buttons had no `onClick` and the
 * backend ignored `range` anyway; and its template ranking counted a project's
 * whole output once per template in that project.
 *
 * Two rules run through everything here.
 *
 * **No data is never drawn as zero.** Every tile is a `Stat`, which carries
 * either a value or the reason it has none -- the same shape the backend's
 * `metrics.Metric` enforces. A spend of $0.00 says "we are not spending
 * anything"; a dash says "nobody has measured this yet". Those are different
 * statements and the second one is usually the true one.
 *
 * **Colour identifies a series, and never alone.** The palette is the six
 * validated categorical slots in `styles.css`, assigned in fixed order so a
 * filter that drops a model does not repaint the others, and every chart with
 * more than one series carries a labelled legend.
 */

import { createFileRoute } from "@tanstack/react-router";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { motion } from "framer-motion";
import {
  Bar, BarChart, CartesianGrid, Cell, LabelList, XAxis, YAxis,
} from "recharts";
import {
  AlertTriangle, BarChart3, CircleDollarSign, FileStack, Info, Layers, Loader2, PieChart,
} from "lucide-react";

import { api } from "@/lib/api";
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import type { ChartConfig } from "@/components/ui/chart";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { ErrorBanner } from "@/components/error-banner";
import {
  DUR, EASE_OUT, FadeIn, staggerDelay, useCountUp, useReducedMotionFlag,
} from "@/components/motion";
import { plainly } from "@/components/processing-banner";
import { PolishedEmpty, SkeletonBar } from "@/components/skeletons";
import { cn } from "@/lib/utils";
import type {
  AnalyticsKpis, AnalyticsRange, CompileReport, CostReport, Stat, TopTemplates, TrendSeries,
} from "@/lib/types";

export const Route = createFileRoute("/_app/analytics")({
  head: () => ({
    meta: [
      { title: "Analytics — DocuMind AI" },
      { name: "description", content: "Documents produced, templates processed, time taken, tokens consumed and what it cost." },
    ],
  }),
  component: AnalyticsPage,
});

const RANGES: { key: AnalyticsRange; label: string }[] = [
  { key: "7d", label: "7 days" },
  { key: "30d", label: "30 days" },
  { key: "90d", label: "90 days" },
  { key: "1y", label: "12 months" },
];

/**
 * The spend buckets in the product's words rather than the engine's.
 *
 * `compile` is one of six grouping keys the server sends, and it was the axis
 * tick and the tooltip heading on this page -- the one place the internal name
 * for reading a template reached a customer's screen through a chart. The keys
 * are left exactly as they arrive: they are what the bars are keyed and ordered
 * on, and only the text a person reads is rewritten.
 */
const OPERATION_LABEL: Record<string, string> = {
  compile: "Processing",
  authoring: "Authoring",
  generate: "Generating",
  edit: "Editing",
  chat: "Chat",
  other: "Other",
};

/**
 * How long a bar takes to grow out of its baseline, in the milliseconds recharts
 * wants.
 *
 * One reveal beat. Recharts' own default is 1500ms, which spends more than half
 * the sequence budget on a bar chart and leaves the page still moving long after
 * the reader has started reading it.
 */
const CHART_MS = DUR.revealSlow * 1000;

/** The page's own step, slower than the shared 35ms primitive. Six tiles at 60ms
 *  read as the page assembling itself; at 35 they read as one block that
 *  flickered. Still far inside the twelve-item cap, so nothing arrives late. */
const STEP_MS = 60;

/** Fixed slot order. A series keeps its colour when its neighbours disappear. */
const SERIES = [
  "var(--color-chart-1)", "var(--color-chart-2)", "var(--color-chart-3)",
  "var(--color-chart-4)", "var(--color-chart-5)", "var(--color-chart-6)",
];

function money(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value === 0) return "$0.00";
  return value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}

function compact(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

function AnalyticsPage() {
  // Checked in code as well as in the stylesheet: recharts drives its growth
  // with JavaScript, and a JS transform does not stop when CSS transitions do.
  const reduced = useReducedMotionFlag();
  const [range, setRange] = useState<AnalyticsRange>("30d");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [kpis, setKpis] = useState<AnalyticsKpis | null>(null);
  const [trend, setTrend] = useState<TrendSeries | null>(null);
  const [byFunction, setByFunction] = useState<{ function: string; count: number }[]>([]);
  const [templates, setTemplates] = useState<TopTemplates | null>(null);
  const [cost, setCost] = useState<CostReport | null>(null);
  const [compiles, setCompiles] = useState<CompileReport | null>(null);

  const load = useCallback((r: AnalyticsRange) => {
    setLoading(true);
    Promise.all([
      api.analyticsKpis(r), api.analyticsTrend(r), api.analyticsByFunction(r),
      api.analyticsTopTemplates(r), api.analyticsCost(r), api.analyticsCompiles(r),
    ])
      .then(([k, t, f, tp, c, cm]) => {
        setKpis(k); setTrend(t); setByFunction(f.items); setTemplates(tp);
        setCost(c); setCompiles(cm);
        setError(null);
      })
      // A banner rather than a toast: these six requests are the entire page, so
      // a failure is the state of the screen and not a passing remark about it.
      .catch((e: any) => setError(e?.message ?? String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(range); }, [range, load]);

  // Skeletons only before anything has ever landed. Switching range keeps the
  // previous window's figures under the small spinner instead, because replacing
  // real numbers with placeholders reads as data loss.
  const firstLoad = loading && kpis == null;

  return (
    <TooltipProvider delayDuration={200}>
      <div className="mx-auto max-w-[1400px] space-y-5 p-6 lg:p-8">
        <FadeIn className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-gradient">Analytics</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              What was produced, how long it took, and what the model calls cost.
            </p>
          </div>
          <div className="flex items-center gap-1.5">
            {loading && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin text-muted-foreground" />}
            {RANGES.map((r) => (
              <button
                key={r.key}
                onClick={() => setRange(r.key)}
                className={cn(
                  "rounded-lg border px-2.5 py-1 text-xs font-medium transition-colors",
                  range === r.key
                    ? "border-primary bg-primary text-primary-foreground"
                    : "border-border hover:bg-accent",
                )}
              >
                {r.label}
              </button>
            ))}
          </div>
        </FadeIn>

        {error && (
          <ErrorBanner
            title="Could not load analytics"
            message={kpis
              ? "The figures below are the last window that did load."
              : "Nothing was read from the server. Your documents are unaffected."}
            detail={error}
            onRetry={() => load(range)}
            retrying={loading}
          />
        )}

        {firstLoad ? <AnalyticsSkeleton /> : kpis == null ? null : (
          <>
            {/* Tiles. Six of them, and the two speed figures §6 asks for --
                `compile_p95_seconds` and `seconds_per_document` -- are already
                among them: the server ranks every tile it can measure and this
                grid renders all of them rather than a hand-picked subset, so a
                measurement added there appears here without a code change. */}
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {kpis.tiles.map((tile, i) => (
                <Reveal key={tile.key} delay={staggerDelay(i, STEP_MS)} className="h-full">
                  <StatTile stat={tile} />
                </Reveal>
              ))}
            </div>

            {cost?.summary.unpriced_calls ? (
              <div className="flex items-start gap-2 rounded-xl border border-ai-uncertain/40 bg-ai-uncertain/10 p-3 text-xs text-ai-uncertain">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>
                  {cost.summary.unpriced_calls} call{cost.summary.unpriced_calls === 1 ? "" : "s"} used a
                  model with no configured rate ({cost.summary.unpriced_models.join(", ")}), so the spend
                  above excludes them. Set a rate in Settings and future calls will be costed — the ones
                  already recorded keep the rate that was in force when they ran.
                </span>
              </div>
            ) : null}

            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              <Panel title="Documents generated"
                     delay={staggerDelay(0, STEP_MS)}
                     subtitle={trend?.granularity === "week" ? "By week, UTC" : "By day, UTC"}>
                {trend && trend.items.some((i) => i.count > 0) ? (
                  <ChartContainer config={{ count: { label: "Documents", color: SERIES[0] } }}
                                  className="h-[220px] w-full">
                    <BarChart data={trend.items} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                      <CartesianGrid vertical={false} strokeOpacity={0.15} />
                      <XAxis dataKey="bucket" tickLine={false} axisLine={false} tickMargin={8}
                             minTickGap={24} tickFormatter={(v: string) => v.slice(5)}
                             className="text-[10px]" />
                      <YAxis tickLine={false} axisLine={false} width={32} allowDecimals={false}
                             className="text-[10px]" />
                      <ChartTooltip content={<ChartTooltipContent />} />
                      {/* Grows out of the axis rather than fading in on top of
                          it, which is the one animation a bar chart owes the
                          reader: the height is the datum, so watching it arrive
                          is watching the figure be stated. */}
                      <Bar dataKey="count" fill="var(--color-count)" radius={[4, 4, 0, 0]}
                           isAnimationActive={!reduced} animationDuration={CHART_MS}
                           animationEasing="ease-out" />
                    </BarChart>
                  </ChartContainer>
                ) : (
                  <Empty icon={<BarChart3 className="h-5 w-5" />} title="Nothing produced yet">
                    No document was generated in this window.
                  </Empty>
                )}
              </Panel>

              <Panel title="Spend over time"
                     delay={staggerDelay(1, STEP_MS)}
                     subtitle={cost?.trend.models.length
                       ? `${cost.trend.models.length} model${cost.trend.models.length === 1 ? "" : "s"}, stacked`
                       : undefined}>
                {cost && cost.trend.models.length > 0 ? (
                  <>
                    <ChartContainer
                      config={Object.fromEntries(cost.trend.models.map((m, i) => [
                        m, { label: m, color: SERIES[i % SERIES.length] },
                      ])) as ChartConfig}
                      className="h-[220px] w-full"
                    >
                      <BarChart data={cost.trend.items} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                        <CartesianGrid vertical={false} strokeOpacity={0.15} />
                        <XAxis dataKey="bucket" tickLine={false} axisLine={false} tickMargin={8}
                               minTickGap={24} tickFormatter={(v: string) => v.slice(5)}
                               className="text-[10px]" />
                        <YAxis tickLine={false} axisLine={false} width={44}
                               tickFormatter={(v: number) => `$${v < 1 ? v.toFixed(2) : v.toFixed(0)}`}
                               className="text-[10px]" />
                        <ChartTooltip content={<ChartTooltipContent />} />
                        {cost.trend.models.map((m, i) => (
                          // A 2px gap between stacked segments, so adjacent fills of
                          // similar hue never read as one block.
                          //
                          // The palette value directly, never `var(--color-${m})`. A
                          // model name is server data and goes into `dataKey`
                          // unchanged, but a CSS custom property name has to be a
                          // <dashed-ident> and `.` is not a valid ident character --
                          // so on a Gemini deployment (`gemini-2.5-pro`,
                          // `gemini-3.6-flash` are the shipped defaults in config.py)
                          // both the declaration and the reference failed to parse,
                          // `fill` fell back to black, and the legend underneath --
                          // which uses this same array inline -- contradicted the
                          // chart it was labelling. The tooltip reads `payload.fill`,
                          // so it follows this and stays correct too.
                          //
                          // Every segment of a stack grows on the same beat.
                          // Staggering them would show the top of a column
                          // hanging in mid-air over a segment that had not
                          // arrived, which reads as a gap in the data.
                          <Bar key={m} dataKey={m} stackId="cost" fill={SERIES[i % SERIES.length]}
                               stroke="var(--color-card)" strokeWidth={2}
                               isAnimationActive={!reduced} animationDuration={CHART_MS}
                               animationEasing="ease-out"
                               radius={i === cost.trend.models.length - 1 ? [4, 4, 0, 0] : 0} />
                        ))}
                      </BarChart>
                    </ChartContainer>
                    {/* The legend is not optional: identity must never be colour
                        alone, and three of the light-mode slots sit under 3:1 on
                        white. */}
                    <Legend items={cost.trend.models.map((m, i) => ({
                      label: m, color: SERIES[i % SERIES.length],
                      value: money(cost.by_model.find((r) => r.key === m)?.cost_usd ?? null),
                    }))} />
                  </>
                ) : (
                  <Empty icon={<CircleDollarSign className="h-5 w-5" />} title="No spend recorded">
                    No model call has been recorded in this window.
                  </Empty>
                )}
              </Panel>

              <Panel title="What the spend was on" subtitle="By operation"
                     delay={staggerDelay(2, STEP_MS)}>
                {cost && cost.by_operation.length > 0 ? (
                  <ChartContainer
                    config={{
                      cost_usd: { label: "Spend" },
                      ...Object.fromEntries(
                        Object.entries(OPERATION_LABEL).map(([key, label]) => [key, { label }]),
                      ),
                    } as ChartConfig}
                    className="h-[200px] w-full"
                  >
                    <BarChart data={cost.by_operation} layout="vertical"
                              margin={{ top: 4, right: 48, left: 8, bottom: 4 }}>
                      <XAxis type="number" hide />
                      <YAxis type="category" dataKey="key" tickLine={false} axisLine={false}
                             width={78} className="text-[11px]"
                             tickFormatter={(v: string) => OPERATION_LABEL[v] ?? plainly(v)} />
                      <ChartTooltip content={<ChartTooltipContent />} />
                      <Bar dataKey="cost_usd" radius={[0, 4, 4, 0]} barSize={18}
                           isAnimationActive={!reduced} animationDuration={CHART_MS}
                           animationEasing="ease-out">
                        {cost.by_operation.map((_row, i) => (
                          <Cell key={i} fill={SERIES[i % SERIES.length]} />
                        ))}
                        <LabelList dataKey="cost_usd" position="right"
                                   className="fill-muted-foreground text-[10px]"
                                   formatter={(v: number) => money(v)} />
                      </Bar>
                    </BarChart>
                  </ChartContainer>
                ) : (
                  <Empty icon={<PieChart className="h-5 w-5" />} title="Nothing to attribute">
                    No priced call has been recorded against an operation yet.
                  </Empty>
                )}
              </Panel>

              <Panel title="Documents by function" subtitle="Across the window"
                     delay={staggerDelay(3, STEP_MS)}>
                {byFunction.length > 0 ? (
                  <div className="space-y-2.5">
                    {/* Hoisted out of the row. It was a full pass over the list
                        per row, i.e. quadratic, for a figure that is the same
                        every time. */}
                    {(() => {
                      const max = Math.max(...byFunction.map((r) => r.count), 1);
                      return byFunction.map((row, i) => (
                        <div key={row.function} className="space-y-1">
                          <div className="flex items-baseline justify-between text-xs">
                            <span className="text-foreground">{row.function}</span>
                            <span className="tabular-nums text-muted-foreground">{row.count}</span>
                          </div>
                          <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                            {/* `scaleX` from a left origin, never `width`: width
                                is laid out and painted every frame, a transform
                                is composited. The bar is full width and scaled
                                down to its share, so the resting geometry is
                                identical to what this drew before. */}
                            <motion.div
                              className="h-full w-full rounded-full"
                              style={{ background: SERIES[i % SERIES.length],
                                       transformOrigin: "left" }}
                              initial={reduced ? false : { scaleX: 0 }}
                              animate={{ scaleX: row.count / max }}
                              transition={{ duration: DUR.revealSlow, ease: EASE_OUT,
                                            delay: reduced ? 0 : staggerDelay(i, STEP_MS) }}
                            />
                          </div>
                        </div>
                      ));
                    })()}
                  </div>
                ) : (
                  <Empty icon={<Layers className="h-5 w-5" />} title="No documents in this window">
                    Nothing has been produced under any function in the window you picked.
                  </Empty>
                )}
              </Panel>
            </div>

            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              <Panel
                title="Most-used templates"
                delay={staggerDelay(0, STEP_MS)}
                subtitle={templates && templates.total_documents > 0
                  ? `Ranked over ${templates.covered_documents} of ${templates.total_documents} documents`
                  : undefined}
              >
                {templates && templates.items.length > 0 ? (
                  <>
                    <div className="divide-y divide-border">
                      {templates.items.map((t, i) => (
                        <div key={t.template_file_id} className="flex items-center gap-3 py-2 text-sm">
                          <span className="w-4 shrink-0 tabular-nums text-xs text-muted-foreground">
                            {i + 1}
                          </span>
                          <span className="min-w-0 flex-1 truncate">{t.name}</span>
                          <span className="shrink-0 tabular-nums text-xs text-muted-foreground">
                            {t.uses} document{t.uses === 1 ? "" : "s"}
                          </span>
                          <span className="w-16 shrink-0 text-right tabular-nums text-xs text-muted-foreground">
                            {money(cost?.by_template.find(
                              (r) => r.template_file_id === t.template_file_id)?.cost_usd ?? null)}
                          </span>
                        </div>
                      ))}
                    </div>
                    {templates.covered_documents < templates.total_documents && (
                      <p className="mt-2 text-[11px] text-muted-foreground">
                        Documents produced outside the template pipeline carry no template record, so
                        they are not ranked here.
                      </p>
                    )}
                  </>
                ) : (
                  <Empty icon={<FileStack className="h-5 w-5" />} title="No template used yet">
                    No template produced a document in this window.
                  </Empty>
                )}
              </Panel>

              <Panel title="Processing templates" subtitle="Reading a template into rules the engine can run"
                     delay={staggerDelay(1, STEP_MS)}>
                <div className="grid grid-cols-2 gap-3">
                  {/* Every `count` below is handed a figure only where one was
                      measured. Where it was not, the prop is undefined, the
                      counter is disabled and the dash stands -- so an absent
                      measurement is never animated into a zero on its way to
                      being reported as nothing. */}
                  <Figure label="Templates processed"
                          count={compiles ? { target: compiles.compiled, format: whole } : undefined} />
                  <Figure label="Runs that stopped"
                          count={compiles ? { target: compiles.failed, format: whole } : undefined}
                          tone={compiles?.failed ? "warn" : undefined} />
                  <Figure
                    label="Median run"
                    count={compiles?.duration?.measured
                      ? { target: compiles.duration.measured.p50_ms / 1000, format: seconds }
                      : undefined}
                    hint={compiles?.duration?.measured ? undefined : "Nothing has been timed yet."} />
                  <Figure
                    label="Slowest run"
                    count={compiles?.duration?.measured
                      ? { target: compiles.duration.measured.worst_ms / 1000, format: seconds }
                      : undefined}
                    hint={compiles?.duration?.measured
                      ? `${compiles.duration.measured.samples} measured`
                      : "Nothing has been timed yet."} />
                </div>
                {compiles?.duration && (
                  <p className="mt-3 text-[11px] text-muted-foreground">
                    Target: {plainly(compiles.duration.target)}.{" "}
                    {compiles.duration.status === "unmeasured"
                      ? "Not measured in this window — the target is a design goal, not a result."
                      : compiles.duration.status === "meeting" ? "Currently met." : "Currently exceeded."}
                  </p>
                )}
              </Panel>
            </div>

            {cost && (
              <Panel title="Token usage" subtitle="Input and output across every model call"
                     delay={staggerDelay(2, STEP_MS)}>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <Figure label="Calls"
                          count={{ target: cost.summary.calls, format: (n) => compact(Math.round(n)) }} />
                  <Figure label="Input tokens"
                          count={{ target: cost.summary.input_tokens, format: (n) => compact(Math.round(n)) }} />
                  <Figure label="Output tokens"
                          count={{ target: cost.summary.output_tokens, format: (n) => compact(Math.round(n)) }} />
                  {/* The one figure here that can be absent. `cost_usd` is null
                      when no call in the window carried a priced model, which is
                      not a spend of nothing -- so it keeps the dash and states
                      the reason rather than counting up to $0.00. */}
                  <Figure label="Total spend"
                          count={cost.summary.cost_usd != null
                            ? { target: cost.summary.cost_usd, format: money }
                            : undefined}
                          hint={cost.summary.cost_usd == null
                            ? "No priced model call in this window."
                            : undefined} />
                </div>
              </Panel>
            )}
          </>
        )}
      </div>
    </TooltipProvider>
  );
}

/**
 * The page at the size it will be.
 *
 * Six tiles and four chart panels, in their real grid at their real heights, so
 * the numbers land into the layout that was already standing rather than
 * pushing it down the screen as each request returns.
 */
function AnalyticsSkeleton() {
  return (
    <>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="rounded-xl surface-raised p-4">
            <SkeletonBar className="h-2.5 w-24" />
            <SkeletonBar className="mt-3 h-7 w-20" />
            <SkeletonBar className="mt-2 h-2 w-32" />
          </div>
        ))}
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="rounded-xl surface-raised p-4">
            <SkeletonBar className="h-3 w-36" />
            <SkeletonBar className="mt-1.5 h-2 w-24" />
            <SkeletonBar className="mt-4 h-[220px] w-full rounded-xl" />
          </div>
        ))}
      </div>
    </>
  );
}

/**
 * One headline number, or an honest statement that there is not one.
 *
 * Props mirror the backend's `Metric.as_dict()` exactly, so the two agree by
 * construction rather than by convention -- and a tile can never print a value
 * while dropping the caveat that came with it.
 */
function StatTile({ stat }: { stat: Stat }) {
  // The count-up is driven off `stat.value` and is disabled when there is not
  // one, so the absence of a measurement can never be animated into a zero on
  // its way to a dash. It is rounded to the precision the server sent, because a
  // rate reported as 4.2 must not spend half a second reading 3.8471629.
  const decimals = (String(stat.value ?? "").split(".")[1] ?? "").length;
  const counted = useCountUp(stat.value ?? 0, 700, stat.value != null);
  const live = Number(counted.toFixed(decimals));

  const shown = stat.value == null
    ? "—"
    // Money is left to `money()`, which picks its own precision: a spend of
    // four ten-thousandths of a cent rounded to the digits of its own decimal
    // string would climb to $0.00 and stop there.
    : stat.unit === "USD" ? money(counted)
    : stat.unit === "%" ? `${live}%`
    : stat.unit === "seconds" ? `${live}s`
    : compact(live);

  return (
    <div className="h-full rounded-xl surface-raised p-4">
      <div className="flex items-start justify-between gap-2">
        <span className="text-xs uppercase tracking-wider text-muted-foreground">
          {plainly(stat.label)}
        </span>
        <Tooltip>
          <TooltipTrigger asChild>
            <button className="text-muted-foreground/60 transition-colors hover:text-foreground">
              <Info className="h-3.5 w-3.5" />
            </button>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs text-xs">
            {plainly(stat.available ? stat.definition : stat.unavailable_reason ?? "")}
          </TooltipContent>
        </Tooltip>
      </div>
      <div className={cn("mt-2 text-2xl font-semibold tabular-nums",
                         !stat.available && "text-muted-foreground/50")}>
        {shown}
      </div>
      {!stat.available && (
        <p className="mt-1 line-clamp-2 text-[11px] leading-snug text-muted-foreground">
          {plainly(stat.unavailable_reason ?? "")}
        </p>
      )}
      {stat.available && stat.key === "documents_generated" && (
        <p className="mt-1 text-[11px] text-muted-foreground">
          {compact(stat.sample.all_time ?? 0)} all time
        </p>
      )}
      {stat.available && stat.key === "tokens_consumed" && (
        <p className="mt-1 text-[11px] text-muted-foreground">
          {compact(stat.sample.input ?? 0)} in · {compact(stat.sample.output ?? 0)} out
        </p>
      )}
    </div>
  );
}

/**
 * One item arriving on this page's beat.
 *
 * Local rather than the shared `StaggerItem`, which fixes its step at 35ms
 * inside a parent `Stagger`. This page wants 60 and wants to hand the delay in
 * per item, so the tiles and the panels below them can share one sequence.
 */
function Reveal({ delay = 0, className, children }: {
  delay?: number; className?: string; children: ReactNode;
}) {
  const reduced = useReducedMotionFlag();
  return (
    <motion.div
      className={className}
      initial={reduced ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: DUR.reveal, ease: EASE_OUT, delay: reduced ? 0 : delay }}
    >
      {children}
    </motion.div>
  );
}

function Panel({ title, subtitle, delay = 0, children }: {
  title: string; subtitle?: string; delay?: number; children: React.ReactNode;
}) {
  return (
    <Reveal delay={delay} className="rounded-xl surface-raised p-4">
      <div className="mb-3">
        <h2 className="text-sm font-medium">{title}</h2>
        {subtitle && <p className="text-[11px] text-muted-foreground">{subtitle}</p>}
      </div>
      {children}
    </Reveal>
  );
}

function Legend({ items }: { items: { label: string; color: string; value?: string }[] }) {
  return (
    <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5">
      {items.map((item) => (
        <span key={item.label} className="inline-flex items-center gap-1.5 text-[11px]">
          <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: item.color }} />
          <span className="text-muted-foreground">{item.label}</span>
          {item.value && <span className="tabular-nums text-foreground">{item.value}</span>}
        </span>
      ))}
    </div>
  );
}

/**
 * A secondary figure, counted up when there is one to count to.
 *
 * `count` is the whole of the honesty rule in one prop: a caller with a
 * measurement passes it, a caller without one does not, and `useCountUp` is
 * disabled in the second case so nothing animates toward a zero that means
 * "nobody measured". `value` is the string for that case and defaults to the
 * dash, which is what every absent figure on this page has always shown.
 */
function Figure({ label, value = "—", hint, tone, count }: {
  label: string;
  value?: string;
  hint?: string;
  tone?: "warn";
  count?: { target: number; format: (n: number) => string };
}) {
  const counted = useCountUp(count?.target ?? 0, 700, count != null);
  const shown = count ? count.format(counted) : value;
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn("mt-0.5 text-lg font-semibold tabular-nums",
                         shown === "—" && "text-muted-foreground/50",
                         tone === "warn" && shown !== "0" && "text-ai-uncertain")}>
        {shown}
      </div>
      {hint && <div className="text-[10px] text-muted-foreground">{hint}</div>}
    </div>
  );
}

/** Formatters for `Figure`'s count-up. Named rather than inlined so a rounding
 *  rule is stated once: a counter mid-flight must print at the precision the
 *  figure will settle at, or the digits churn on their way to a stable number. */
const whole = (n: number) => String(Math.round(n));
const seconds = (n: number) => `${n.toFixed(1)}s`;

/** A panel with nothing in it yet -- never a panel whose request failed, which
 *  is `ErrorBanner`'s job at the top of the page.
 *
 *  The height floor is what it was when this was a line of grey text: a window
 *  with no data must not resize the grid around it when the next window has
 *  some. */
function Empty({ icon, title, children }: {
  icon: ReactNode; title: string; children: string;
}) {
  return (
    <PolishedEmpty
      icon={icon}
      title={title}
      subtitle={children}
      className="min-h-[180px] border-0 px-4 py-6"
    />
  );
}
