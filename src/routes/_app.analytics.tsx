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
import { useCallback, useEffect, useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Cell, LabelList, XAxis, YAxis,
} from "recharts";
import { AlertTriangle, Info, Loader2 } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import type { ChartConfig } from "@/components/ui/chart";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type {
  AnalyticsKpis, AnalyticsRange, CompileReport, CostReport, Stat, TopTemplates, TrendSeries,
} from "@/lib/types";

export const Route = createFileRoute("/_app/analytics")({
  head: () => ({
    meta: [
      { title: "Analytics — DocuMind AI" },
      { name: "description", content: "Documents produced, templates compiled, time taken, tokens consumed and what it cost." },
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
  const [range, setRange] = useState<AnalyticsRange>("30d");
  const [loading, setLoading] = useState(true);
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
      })
      .catch((e: any) => toast.error("Could not load analytics", { description: e?.message ?? String(e) }))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(range); }, [range, load]);

  return (
    <TooltipProvider delayDuration={200}>
      <div className="mx-auto max-w-[1400px] space-y-5 p-6 lg:p-8">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Analytics</h1>
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
        </div>

        {/* Tiles */}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {(kpis?.tiles ?? []).map((tile) => <StatTile key={tile.key} stat={tile} />)}
        </div>

        {cost?.summary.unpriced_calls ? (
          <div className="flex items-start gap-2 rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-500">
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
                  <Bar dataKey="count" fill="var(--color-count)" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ChartContainer>
            ) : (
              <Empty>No documents were generated in this window.</Empty>
            )}
          </Panel>

          <Panel title="Spend over time"
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
                      <Bar key={m} dataKey={m} stackId="cost" fill={SERIES[i % SERIES.length]}
                           stroke="var(--color-card)" strokeWidth={2}
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
              <Empty>No model call has been recorded in this window.</Empty>
            )}
          </Panel>

          <Panel title="What the spend was on" subtitle="By operation">
            {cost && cost.by_operation.length > 0 ? (
              <ChartContainer config={{ cost_usd: { label: "Spend" } }} className="h-[200px] w-full">
                <BarChart data={cost.by_operation} layout="vertical"
                          margin={{ top: 4, right: 48, left: 8, bottom: 4 }}>
                  <XAxis type="number" hide />
                  <YAxis type="category" dataKey="key" tickLine={false} axisLine={false}
                         width={78} className="text-[11px]" />
                  <ChartTooltip content={<ChartTooltipContent />} />
                  <Bar dataKey="cost_usd" radius={[0, 4, 4, 0]} barSize={18}>
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
              <Empty>Nothing to attribute yet.</Empty>
            )}
          </Panel>

          <Panel title="Documents by function" subtitle="Across the window">
            {byFunction.length > 0 ? (
              <div className="space-y-2.5">
                {byFunction.map((row, i) => {
                  const max = Math.max(...byFunction.map((r) => r.count), 1);
                  return (
                    <div key={row.function} className="space-y-1">
                      <div className="flex items-baseline justify-between text-xs">
                        <span className="text-foreground">{row.function}</span>
                        <span className="tabular-nums text-muted-foreground">{row.count}</span>
                      </div>
                      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                        <div className="h-full rounded-full"
                             style={{ width: `${(row.count / max) * 100}%`,
                                      background: SERIES[i % SERIES.length] }} />
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <Empty>No documents in this window.</Empty>
            )}
          </Panel>
        </div>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Panel
            title="Most-used templates"
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
                    Documents produced outside the manifest pipeline carry no template record, so
                    they are not ranked here.
                  </p>
                )}
              </>
            ) : (
              <Empty>No template produced a document in this window.</Empty>
            )}
          </Panel>

          <Panel title="Compiling" subtitle="Reading a template into a manifest">
            <div className="grid grid-cols-2 gap-3">
              <Figure label="Templates compiled" value={compiles ? String(compiles.compiled) : "—"} />
              <Figure label="Compiles that failed"
                      value={compiles ? String(compiles.failed) : "—"}
                      tone={compiles?.failed ? "warn" : undefined} />
              <Figure
                label="Median compile"
                value={compiles?.duration?.measured
                  ? `${(compiles.duration.measured.p50_ms / 1000).toFixed(1)}s` : "—"}
                hint={compiles?.duration?.measured ? undefined : "Nothing has been timed yet."} />
              <Figure
                label="Slowest compile"
                value={compiles?.duration?.measured
                  ? `${(compiles.duration.measured.worst_ms / 1000).toFixed(1)}s` : "—"}
                hint={compiles?.duration?.measured
                  ? `${compiles.duration.measured.samples} measured`
                  : "Nothing has been timed yet."} />
            </div>
            {compiles?.duration && (
              <p className="mt-3 text-[11px] text-muted-foreground">
                Target: {compiles.duration.target}.{" "}
                {compiles.duration.status === "unmeasured"
                  ? "Not measured in this window — the target is a design goal, not a result."
                  : compiles.duration.status === "meeting" ? "Currently met." : "Currently exceeded."}
              </p>
            )}
          </Panel>
        </div>

        {cost && (
          <Panel title="Token usage" subtitle="Input and output across every model call">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Figure label="Calls" value={compact(cost.summary.calls)} />
              <Figure label="Input tokens" value={compact(cost.summary.input_tokens)} />
              <Figure label="Output tokens" value={compact(cost.summary.output_tokens)} />
              <Figure label="Total spend" value={money(cost.summary.cost_usd)} />
            </div>
          </Panel>
        )}
      </div>
    </TooltipProvider>
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
  const shown = stat.value == null
    ? "—"
    : stat.unit === "USD" ? money(stat.value)
    : stat.unit === "%" ? `${stat.value}%`
    : stat.unit === "seconds" ? `${stat.value}s`
    : compact(stat.value);

  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="flex items-start justify-between gap-2">
        <span className="text-xs uppercase tracking-wider text-muted-foreground">{stat.label}</span>
        <Tooltip>
          <TooltipTrigger asChild>
            <button className="text-muted-foreground/60 transition-colors hover:text-foreground">
              <Info className="h-3.5 w-3.5" />
            </button>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs text-xs">
            {stat.available ? stat.definition : stat.unavailable_reason}
          </TooltipContent>
        </Tooltip>
      </div>
      <div className={cn("mt-2 text-2xl font-semibold tabular-nums",
                         !stat.available && "text-muted-foreground/50")}>
        {shown}
      </div>
      {!stat.available && (
        <p className="mt-1 line-clamp-2 text-[11px] leading-snug text-muted-foreground">
          {stat.unavailable_reason}
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

function Panel({ title, subtitle, children }: {
  title: string; subtitle?: string; children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="mb-3">
        <h2 className="text-sm font-medium">{title}</h2>
        {subtitle && <p className="text-[11px] text-muted-foreground">{subtitle}</p>}
      </div>
      {children}
    </div>
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

function Figure({ label, value, hint, tone }: {
  label: string; value: string; hint?: string; tone?: "warn";
}) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn("mt-0.5 text-lg font-semibold tabular-nums",
                         value === "—" && "text-muted-foreground/50",
                         tone === "warn" && value !== "0" && "text-amber-500")}>
        {value}
      </div>
      {hint && <div className="text-[10px] text-muted-foreground">{hint}</div>}
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-[180px] items-center justify-center px-6 text-center text-xs text-muted-foreground">
      {children}
    </div>
  );
}
