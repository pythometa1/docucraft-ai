import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { CheckCircle2, ClipboardCheck, Loader2, X } from "lucide-react";
import { api, type ReviewTask } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/_app/review")({
  head: () => ({
    meta: [
      { title: "Review queue — DocuMind AI" },
      { name: "description", content: "Values the engine would not guess: calculations needing sign-off, ambiguous conditions, and poorly-grounded narrative." },
    ],
  }),
  component: ReviewQueue,
});

const KIND_TONE: Record<string, string> = {
  calculation: "text-purple border-purple/40 bg-purple/10",
  condition: "text-warning border-warning/40 bg-warning/10",
  narrative: "text-brand border-brand/40 bg-brand/10",
  binding: "text-info border-info/40 bg-info/10",
};

function ReviewQueue() {
  const [tasks, setTasks] = useState<ReviewTask[]>([]);
  const [summary, setSummary] = useState<any>(null);
  const [selected, setSelected] = useState<ReviewTask | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<"open" | "resolved">("open");

  const refresh = (status: "open" | "resolved" = filter) => {
    setLoading(true);
    Promise.all([api.reviewTasks({ status }), api.reviewSummary()])
      .then(([list, s]) => {
        setTasks(list.items);
        setSummary(s);
        setSelected((current) => list.items.find((t) => t.id === current?.id) ?? list.items[0] ?? null);
      })
      .catch((e: any) => toast.error("Could not load the review queue", { description: e?.message ?? String(e) }))
      .finally(() => setLoading(false));
  };

  useEffect(() => { refresh(filter); }, [filter]);

  return (
    <div className="p-6 lg:p-8 max-w-7xl mx-auto space-y-6">
      <div className="flex items-start justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Review queue</h1>
          <p className="text-sm text-muted-foreground mt-1 max-w-2xl">
            Where the engine stopped rather than guessed. A wrong number in a regulated document is worse
            than a slow one, so anything it couldn't resolve confidently waits here for a person.
          </p>
        </div>
        <div className="flex gap-2">
          {(["open", "resolved"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={cn(
                "px-3 py-1.5 rounded-md text-xs font-medium border capitalize",
                filter === f ? "bg-primary text-primary-foreground border-primary" : "border-border hover:bg-muted",
              )}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {summary && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          {[
            ["Open", summary.open],
            ["Resolved", summary.resolved],
            ["Dismissed", summary.dismissed],
            ["Human-touch rate", summary.open + summary.resolved > 0
              ? `${Math.round((summary.resolved / (summary.open + summary.resolved)) * 100)}%`
              : "—"],
          ].map(([label, value]) => (
            <div key={String(label)} className="rounded-xl border border-border bg-card p-4">
              <div className="text-xs text-muted-foreground uppercase tracking-wider">{label}</div>
              <div className="text-2xl font-semibold mt-1 tabular-nums">{value}</div>
            </div>
          ))}
        </div>
      )}

      <div className="grid lg:grid-cols-[380px_1fr] gap-5">
        <div className="rounded-xl border border-border bg-card overflow-hidden flex flex-col max-h-[70vh]">
          <div className="px-4 py-3 border-b border-border text-xs uppercase tracking-wider text-muted-foreground">
            {tasks.length} task{tasks.length === 1 ? "" : "s"}
          </div>
          <div className="flex-1 overflow-auto divide-y divide-border">
            {loading && <div className="p-6 text-sm text-muted-foreground flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</div>}
            {!loading && tasks.length === 0 && (
              <div className="p-8 text-center">
                <CheckCircle2 className="h-8 w-8 text-success mx-auto mb-3" />
                <div className="font-medium">Nothing waiting</div>
                <p className="text-sm text-muted-foreground mt-1">
                  Every unit resolved on its own.
                </p>
              </div>
            )}
            {tasks.map((t) => (
              <button
                key={t.id}
                onClick={() => setSelected(t)}
                className={cn("w-full text-left px-4 py-3 hover:bg-accent/40", selected?.id === t.id && "bg-accent/60")}
              >
                <div className="flex items-center gap-2 mb-1 flex-wrap">
                  <Badge variant="outline" className={cn("text-[10px]", KIND_TONE[t.kind])}>{t.kind}</Badge>
                  <code className="text-[11px] font-mono text-muted-foreground truncate">{t.unit_id}</code>
                </div>
                <div className="text-sm line-clamp-2">{t.question}</div>
              </button>
            ))}
          </div>
        </div>

        {selected ? <TaskDetail key={selected.id} task={selected} onDone={() => refresh(filter)} /> : (
          <div className="rounded-xl border border-dashed border-border p-10 text-center text-sm text-muted-foreground">
            Select a task to resolve it.
          </div>
        )}
      </div>
    </div>
  );
}

function TaskDetail({ task, onDone }: { task: ReviewTask; onDone: () => void }) {
  const [value, setValue] = useState(task.proposed_value ?? "");
  const [rationale, setRationale] = useState("");
  const [promote, setPromote] = useState(false);
  const [busy, setBusy] = useState(false);
  const readOnly = task.status !== "open";

  const resolve = async () => {
    if (!rationale.trim()) {
      toast.error("A rationale is required", { description: "It becomes part of the document's audit record." });
      return;
    }
    setBusy(true);
    try {
      const r = await api.resolveReviewTask(task.id, {
        resolved_value: value || undefined,
        rationale,
        promote_to_manifest: promote,
        promote_as: task.kind === "calculation" ? "formula" : "condition_expression",
      });
      toast.success("Resolved", {
        description: r.promoted?.applied
          ? "Promoted into the manifest — this won't be asked again."
          : r.promoted?.reason,
      });
      onDone();
    } catch (e: any) {
      toast.error("Could not resolve", { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  };

  const dismiss = async () => {
    try {
      await api.dismissReviewTask(task.id);
      toast("Dismissed");
      onDone();
    } catch (e: any) {
      toast.error("Could not dismiss", { description: e?.message ?? String(e) });
    }
  };

  const context = task.context ?? {};

  return (
    <div className="rounded-xl border border-border bg-card p-6 space-y-5">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <Badge variant="outline" className={cn("text-[10px] mb-2", KIND_TONE[task.kind])}>{task.kind}</Badge>
          <h2 className="text-lg font-semibold">{task.question}</h2>
          <code className="text-xs font-mono text-muted-foreground">{task.unit_id}</code>
        </div>
        {!readOnly && (
          <Button variant="ghost" size="sm" onClick={dismiss} className="text-muted-foreground">
            <X className="h-4 w-4 mr-1.5" /> Dismiss
          </Button>
        )}
      </div>

      {context.expression && (
        <div>
          <div className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">Expression</div>
          <code className="block rounded-lg border border-border bg-background/40 p-3 text-sm font-mono break-all">
            {context.expression}
          </code>
        </div>
      )}

      {(context.inputs || context.available) && (
        <div>
          <div className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">Values it had</div>
          <div className="rounded-lg border border-border divide-y divide-border text-sm">
            {Object.entries({ ...(context.available ?? {}), ...(context.inputs ?? {}) }).map(([k, v]) => (
              <div key={k} className="flex items-center justify-between gap-3 px-3 py-2">
                <code className="text-xs font-mono text-brand">{k}</code>
                <span className={cn("truncate", v == null && "text-destructive")}>{v == null ? "(missing)" : String(v)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {readOnly ? (
        <div className="rounded-lg border border-success/30 bg-success/10 p-4 text-sm space-y-1">
          <div><span className="font-medium">Resolved to:</span> {task.resolved_value || "—"}</div>
          <div className="text-muted-foreground">{task.rationale}</div>
        </div>
      ) : (
        <>
          <div>
            <div className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">
              {task.kind === "calculation" ? "Value or corrected formula" : "Resolved value"}
            </div>
            <Input value={value} onChange={(e) => setValue(e.target.value)} className="font-mono text-sm" placeholder="e.g. 7.14" />
          </div>

          <div>
            <div className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">Rationale (required)</div>
            <Textarea
              rows={3}
              value={rationale}
              onChange={(e) => setRationale(e.target.value)}
              placeholder="Why this value is correct — this is recorded in the document's lineage."
            />
          </div>

          <div className="flex items-center justify-between rounded-lg border border-border p-3">
            <div>
              <div className="text-sm font-medium">Promote into the manifest</div>
              <div className="text-xs text-muted-foreground">
                Turn this decision into a rule so the same question isn't asked on every future document.
              </div>
            </div>
            <Switch checked={promote} onCheckedChange={setPromote} />
          </div>

          <div className="flex justify-end">
            <Button onClick={resolve} disabled={busy} className="bg-gradient-brand text-white">
              {busy ? <Loader2 className="h-4 w-4 mr-1.5 animate-spin" /> : <ClipboardCheck className="h-4 w-4 mr-1.5" />}
              Resolve
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
