import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { TrendingUp, TrendingDown, FileText, Zap, Clock, CheckCircle2 } from "lucide-react";
import { api } from "@/lib/api";

export const Route = createFileRoute("/_app/analytics")({
  head: () => ({ meta: [{ title: "Analytics — DocuMind AI" }, { name: "description", content: "Usage analytics and KPIs." }] }),
  component: AnalyticsPage,
});

function AnalyticsPage() {
  const [kpis, setKpis] = useState<any>(null);
  const [trend, setTrend] = useState<{ day: string; count: number }[]>([]);
  const [byFunction, setByFunction] = useState<{ function: string; count: number }[]>([]);
  const [topTemplates, setTopTemplates] = useState<{ name: string; uses: number }[]>([]);

  useEffect(() => {
    Promise.all([api.analyticsKpis(), api.analyticsTrend(), api.analyticsByFunction(), api.analyticsTopTemplates()])
      .then(([k, t, f, tt]) => { setKpis(k); setTrend(t.items); setByFunction(f.items); setTopTemplates(tt.items); })
      .catch((e: any) => toast.error("Could not load analytics", { description: e?.message ?? String(e) }));
  }, []);

  const liveKpis = [
    { label: "Documents generated", value: kpis ? String(kpis.documents_generated) : "—", icon: FileText, hint: "all time" },
    { label: "AI tokens consumed", value: kpis ? kpis.ai_tokens_consumed.toLocaleString() : "—", icon: Zap, hint: "across all jobs" },
    { label: "Avg. time to draft", value: kpis ? `${kpis.avg_time_to_draft_seconds}s` : "—", icon: Clock, hint: "per generation job" },
    { label: "Approval rate", value: kpis ? `${kpis.approval_rate_pct}%` : "—", icon: CheckCircle2, hint: "of document versions" },
  ];
  const trendCounts = trend.map((t) => t.count);
  const max = Math.max(1, ...trendCounts);
  const totalByFunction = Math.max(1, byFunction.reduce((sum, f) => sum + f.count, 0));
  return (
    <div className="p-6 lg:p-8 max-w-7xl mx-auto space-y-6">
      <div className="flex items-start justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Analytics</h1>
          <p className="text-sm text-muted-foreground mt-1">Workspace usage, throughput, and quality signals.</p>
        </div>
        <div className="flex gap-2">
          {["7d", "30d", "90d", "1y"].map((r, i) => (
            <button
              key={r}
              className={
                "px-3 py-1.5 rounded-md text-xs font-medium border " +
                (i === 1 ? "bg-primary text-primary-foreground border-primary" : "border-border hover:bg-muted")
              }
            >
              {r}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
        {liveKpis.map((k) => (
          <div key={k.label} className="rounded-xl border border-border bg-card p-5">
            <div className="flex items-center justify-between">
              <div className="h-9 w-9 rounded-lg bg-primary/10 text-primary flex items-center justify-center">
                <k.icon className="h-4 w-4" />
              </div>

            </div>
            <div className="text-2xl font-semibold mt-4 tabular-nums">{k.value}</div>
            <div className="text-xs text-muted-foreground mt-1">{k.label}</div>
            <div className="text-[11px] text-muted-foreground/70 mt-0.5">{k.hint}</div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 rounded-xl border border-border bg-card p-5">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="font-semibold">Documents generated</h2>
              <p className="text-xs text-muted-foreground">Last 14 days</p>
            </div>
            <div className="text-right">
              <div className="text-2xl font-semibold tabular-nums">{trendCounts.reduce((a, b) => a + b, 0)}</div>
              
            </div>
          </div>
          <div className="h-56 mt-6 flex items-end gap-2">
            {trendCounts.map((v, i) => (
              <div key={i} className="flex-1 flex flex-col items-center gap-1.5">
                <div
                  className="w-full rounded-t-md bg-gradient-to-t from-primary to-purple-500 hover:opacity-80 transition-opacity"
                  style={{ height: `${(v / max) * 100}%` }}
                  title={`${v} docs`}
                />
                <div className="text-[10px] text-muted-foreground">{trend[i]?.day?.slice(-2)}</div>
              </div>
            ))}
          </div>
        </div>

        <div className="rounded-xl border border-border bg-card p-5">
          <h2 className="font-semibold">By function</h2>
          <p className="text-xs text-muted-foreground">Distribution this month</p>
          <div className="space-y-4 mt-5">
            {byFunction.map((f) => (
              <div key={f.function}>
                <div className="flex items-center justify-between text-sm">
                  <span className="truncate">{f.function}</span>
                  <span className="text-muted-foreground tabular-nums">{f.count}</span>
                </div>
                <div className="mt-1.5 h-2 rounded-full bg-muted overflow-hidden">
                  <div className="h-full bg-gradient-brand" style={{ width: `${(f.count / totalByFunction) * 100}%` }} />
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-border bg-card">
        <div className="p-5 border-b border-border">
          <h2 className="font-semibold">Top templates</h2>
          <p className="text-xs text-muted-foreground">Ranked by usage this month</p>
        </div>
        <div className="divide-y divide-border">
          {topTemplates.map((t, i) => (
            <div key={t.name} className="p-4 flex items-center gap-4">
              <div className="h-8 w-8 rounded-lg bg-muted flex items-center justify-center text-xs font-semibold text-muted-foreground">
                {i + 1}
              </div>
              <div className="flex-1 min-w-0">
                <div className="font-medium truncate">{t.name}</div>
                <div className="text-xs text-muted-foreground">{t.uses} generations</div>
              </div>
              
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
