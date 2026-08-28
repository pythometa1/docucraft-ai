import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { Download, Filter, Search, FileText, UserCog, ShieldCheck, Trash2, Upload, CheckCircle2, LogIn, Sparkles } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/_app/audit-log")({
  head: () => ({ meta: [{ title: "Audit Log — DocuMind AI" }, { name: "description", content: "Compliance-grade audit trail." }] }),
  component: AuditLogPage,
});

type Sev = "info" | "success" | "warning" | "danger";
type Entry = { time: string; actor: string; action: string; target: string; severity: Sev; entity_type: string };


const sevTone: Record<Sev, string> = {
  info: "bg-blue-500/10 text-blue-500",
  success: "bg-emerald-500/10 text-emerald-500",
  warning: "bg-amber-500/10 text-amber-500",
  danger: "bg-red-500/10 text-red-500",
};

function AuditLogPage() {
  const [entries, setEntries] = useState<Entry[]>([]);

  useEffect(() => {
    api.auditLogs()
      .then((r) => setEntries(r.items.map((e: any) => ({
        time: e.time ? new Date(e.time).toLocaleString() : "—",
        actor: e.actor ?? "System",
        action: e.action,
        target: e.target ?? "—",
        severity: (e.severity ?? "info") as Sev,
        entity_type: e.entity_type ?? "",
      }))))
      .catch((e: any) => toast.error("Could not load the audit log", { description: e?.message ?? String(e) }));
  }, []);

  const ENTRIES = entries;
  return (
    <div className="p-6 lg:p-8 max-w-7xl mx-auto space-y-6">
      <div className="flex items-start justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Audit Log</h1>
          <p className="text-sm text-muted-foreground mt-1">Immutable, compliance-grade trail of every workspace action. Retained 7 years.</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" className="gap-2"><Filter className="h-4 w-4" /> Filter</Button>
          <Button size="sm" className="gap-2"><Download className="h-4 w-4" /> Export CSV</Button>
        </div>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {[
          { label: "Events", value: String(ENTRIES.length) },
          { label: "Approvals", value: String(ENTRIES.filter((e) => e.action.toLowerCase().includes("approv")).length) },
          { label: "Generations", value: String(ENTRIES.filter((e) => e.action.toLowerCase().includes("generat")).length) },
          { label: "Warnings", value: String(ENTRIES.filter((e) => e.severity === "warning").length) },
        ].map((s) => (
          <div key={s.label} className="rounded-xl border border-border bg-card p-4">
            <div className="text-xs text-muted-foreground uppercase tracking-wider">{s.label}</div>
            <div className="text-2xl font-semibold mt-1 tabular-nums">{s.value}</div>
          </div>
        ))}
      </div>

      <div className="rounded-xl border border-border bg-card">
        <div className="p-4 border-b border-border flex items-center gap-3">
          <div className="relative flex-1 max-w-md">
            <Search className="h-4 w-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <Input placeholder="Search actor, action, target…" className="pl-9" />
          </div>
          <div className="ml-auto text-xs text-muted-foreground">Showing {ENTRIES.length} events</div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-muted/40">
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="px-4 py-3 font-medium whitespace-nowrap">Timestamp (UTC)</th>
                <th className="px-4 py-3 font-medium whitespace-nowrap">Actor</th>
                <th className="px-4 py-3 font-medium">Action</th>
                <th className="px-4 py-3 font-medium">Target</th>
                <th className="px-4 py-3 font-medium whitespace-nowrap">Entity</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {ENTRIES.map((e, i) => (
                <tr key={i} className="hover:bg-muted/30 transition-colors">
                  <td className="px-4 py-3 font-mono text-xs text-muted-foreground whitespace-nowrap">{e.time}</td>
                  <td className="px-4 py-3 whitespace-nowrap">{e.actor}</td>
                  <td className="px-4 py-3">
                    <span className={cn("inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md text-xs font-medium", sevTone[e.severity])}>
                      {e.action}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">{e.target}</td>
                  <td className="px-4 py-3 font-mono text-xs text-muted-foreground whitespace-nowrap">{e.entity_type}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="p-4 border-t border-border flex items-center justify-between text-xs text-muted-foreground">
          <span>Most recent first</span>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled>Previous</Button>
            <Button variant="outline" size="sm">Next</Button>
          </div>
        </div>
      </div>
    </div>
  );
}
