import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import {
  ArrowLeft, Check, ChevronRight, Download, FileSearch, Link2, Loader2,
  Play, RefreshCcw, ShieldCheck, Sparkles, Table2,
} from "lucide-react";
import { api, type BindingSuggestion, type ManifestPreview } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/_app/projects/$id_/studio/$templateId")({
  head: () => ({
    meta: [
      { title: "Template Studio — DocuMind AI" },
      { name: "description", content: "Compile a Word template into an executable manifest, review what the compiler understood, bind it to your data, and generate." },
    ],
  }),
  component: TemplateStudio,
});

const STEPS = [
  { key: "compile", n: 1, title: "Compile", icon: Sparkles, hint: "Read the template's own rules" },
  { key: "review", n: 2, title: "Review", icon: FileSearch, hint: "Confirm what the compiler understood" },
  { key: "bind", n: 3, title: "Bind", icon: Link2, hint: "Map spreadsheet columns to fields" },
  { key: "generate", n: 4, title: "Generate", icon: Play, hint: "Preview, then run the batch" },
] as const;

type StepKey = (typeof STEPS)[number]["key"];

function TemplateStudio() {
  const { id: projectId, templateId } = Route.useParams();
  const navigate = useNavigate();

  const [step, setStep] = useState<StepKey>("compile");
  const [manifest, setManifest] = useState<any | null>(null);
  const [manifests, setManifests] = useState<any[]>([]);
  const [templateName, setTemplateName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.getTemplate(templateId), api.listManifests(templateId)])
      .then(([template, list]) => {
        setTemplateName(template.name);
        setManifests(list.items);
        if (list.items[0]) {
          setManifest(list.items[0]);
          setStep("review");
        }
      })
      .catch((e: any) => setLoadError(e?.message ?? String(e)));
  }, [templateId]);

  const compile = async (agentic: boolean) => {
    setBusy(agentic ? "agentic" : "compile");
    try {
      const compiled = await api.compileManifest(templateId, { agentic, refine: !agentic });
      setManifest(compiled);
      setManifests((prev) => [compiled, ...prev]);
      toast.success(`Compiled v${compiled.version_no}`, {
        description: `${compiled.fields.length} fields · ${compiled.conditions.length} conditions · ${compiled.blocks.length} blocks`,
      });
      setStep("review");
    } catch (e: any) {
      toast.error("Could not compile this template", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  };

  if (loadError) {
    return (
      <div className="p-8 max-w-lg mx-auto text-center space-y-3">
        <p className="text-muted-foreground">{loadError}</p>
        <Link to="/dashboard" className="text-brand hover:underline">Back to dashboard</Link>
      </div>
    );
  }

  const activeIndex = STEPS.findIndex((s) => s.key === step);

  return (
    <div className="p-6 md:p-8 max-w-7xl mx-auto space-y-6">
      <div className="text-sm text-muted-foreground flex items-center gap-2 flex-wrap">
        <Link to="/dashboard" className="hover:text-foreground">Projects</Link>
        <ChevronRight className="h-3.5 w-3.5" />
        <Link to="/projects/$id" params={{ id: projectId }} className="hover:text-foreground">Project</Link>
        <ChevronRight className="h-3.5 w-3.5" />
        <span className="text-foreground">Template Studio</span>
      </div>

      <div className="rounded-2xl border border-border bg-surface p-6 flex items-start justify-between gap-4 flex-wrap">
        <div className="min-w-0">
          <div className="text-[11px] uppercase tracking-wider text-muted-foreground font-mono">Template</div>
          <h1 className="text-2xl font-bold tracking-tight truncate">{templateName || "Loading…"}</h1>
          {manifest && (
            <div className="flex items-center gap-2 mt-2 flex-wrap">
              <Badge variant="outline">v{manifest.version_no}</Badge>
              <StatusPill status={manifest.status} />
              <ConfidenceBadge value={manifest.confidence} />
              <span className="text-xs text-muted-foreground font-mono">{manifest.compiled_by}</span>
            </div>
          )}
        </div>
        <button
          onClick={() => navigate({ to: "/projects/$id", params: { id: projectId } })}
          className="h-9 px-3 rounded-lg border border-border hover:bg-accent text-sm inline-flex items-center gap-1.5"
        >
          <ArrowLeft className="h-4 w-4" /> Back to project
        </button>
      </div>

      <div className="rounded-2xl border border-border bg-surface p-4">
        <div className="grid grid-cols-4 gap-3">
          {STEPS.map((s, i) => {
            const reached = manifest !== null || s.key === "compile";
            const isActive = step === s.key;
            return (
              <button
                key={s.key}
                disabled={!reached}
                onClick={() => setStep(s.key)}
                className="flex flex-col items-center text-center gap-2 group disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <div className={cn(
                  "h-10 w-10 rounded-full flex items-center justify-center border-2 font-mono text-sm font-semibold transition-all",
                  isActive
                    ? "bg-gradient-brand text-white border-transparent shadow-lg shadow-brand/30 scale-110"
                    : i < activeIndex
                      ? "bg-success/10 text-success border-success/40"
                      : "bg-background text-muted-foreground border-border",
                )}>
                  {i < activeIndex ? <Check className="h-4 w-4" /> : s.n}
                </div>
                <div>
                  <div className={cn("text-sm font-semibold", isActive ? "text-foreground" : "text-muted-foreground")}>{s.title}</div>
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono">{s.hint}</div>
                </div>
              </button>
            );
          })}
        </div>
      </div>

      {step === "compile" && <CompileStep manifests={manifests} busy={busy} onCompile={compile} onPick={(m) => { setManifest(m); setStep("review"); }} />}
      {step === "review" && manifest && <ReviewStep manifest={manifest} onManifestChange={setManifest} onNext={() => setStep("bind")} />}
      {step === "bind" && manifest && <BindStep projectId={projectId} manifest={manifest} onNext={() => setStep("generate")} />}
      {step === "generate" && manifest && <GenerateStep projectId={projectId} manifest={manifest} />}
    </div>
  );
}

/* ------------------------------------------------------------------ shared */
function StatusPill({ status }: { status: string }) {
  const tone: Record<string, string> = {
    draft: "bg-muted text-muted-foreground border-border",
    in_review: "bg-warning/10 text-warning border-warning/30",
    approved: "bg-success/10 text-success border-success/30",
    deprecated: "bg-destructive/10 text-destructive border-destructive/30",
  };
  return (
    <span className={cn("inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium", tone[status] ?? tone.draft)}>
      {status.replace("_", " ")}
    </span>
  );
}

/** Confidence is what drives a reviewer's attention, so it is shown wherever a
 * compiler decision appears rather than buried in the manifest JSON. */
function ConfidenceBadge({ value }: { value: number }) {
  const tone = value >= 0.9 ? "text-success border-success/40 bg-success/10"
    : value >= 0.7 ? "text-warning border-warning/40 bg-warning/10"
    : "text-destructive border-destructive/40 bg-destructive/10";
  return <Badge variant="outline" className={cn("text-[10px]", tone)}>{Math.round(value * 100)}% confident</Badge>;
}

function Panel({ title, description, children, actions }: { title: string; description?: string; children: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <div className="rounded-2xl border border-border bg-surface">
      <div className="flex items-start justify-between gap-4 p-6 border-b border-border flex-wrap">
        <div>
          <h2 className="text-lg font-semibold">{title}</h2>
          {description && <p className="text-sm text-muted-foreground mt-1 max-w-2xl">{description}</p>}
        </div>
        {actions}
      </div>
      <div className="p-6">{children}</div>
    </div>
  );
}

/* ------------------------------------------------------------- step 1 */
function CompileStep({ manifests, busy, onCompile, onPick }: {
  manifests: any[]; busy: string | null;
  onCompile: (agentic: boolean) => void; onPick: (m: any) => void;
}) {
  return (
    <Panel
      title="Compile the template"
      description="The template's own rules — which text is a placeholder, which sections are conditional — get read once and turned into an executable manifest. Every document generated afterwards runs that manifest deterministically."
    >
      <div className="grid md:grid-cols-2 gap-3">
        <button
          onClick={() => onCompile(false)}
          disabled={!!busy}
          className="text-left rounded-lg border border-border bg-background/40 p-4 hover:border-border-strong disabled:opacity-50"
        >
          <div className="flex items-center gap-2 font-semibold">
            {busy === "compile" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4 text-brand" />}
            Compile
          </div>
          <p className="text-sm text-muted-foreground mt-1">
            Rules first for anything colour-coded or bracketed; the model reads the instruction prose,
            in whatever language it's written.
          </p>
        </button>
        <button
          onClick={() => onCompile(true)}
          disabled={!!busy}
          className="text-left rounded-lg border border-border bg-background/40 p-4 hover:border-border-strong disabled:opacity-50"
        >
          <div className="flex items-center gap-2 font-semibold">
            {busy === "agentic" ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCcw className="h-4 w-4 text-purple" />}
            Compile &amp; self-verify
          </div>
          <p className="text-sm text-muted-foreground mt-1">
            Compiles, test-fills the result, reads its own quality failures and revises — up to three rounds.
            Slower, and worth it for a template family you'll reuse.
          </p>
        </button>
      </div>

      {manifests.length > 0 && (
        <div className="mt-6">
          <div className="text-xs uppercase tracking-wider text-muted-foreground mb-2">Existing versions</div>
          <div className="rounded-lg border border-border divide-y divide-border">
            {manifests.map((m) => (
              <button key={m.id} onClick={() => onPick(m)} className="w-full text-left p-3 hover:bg-accent/40 flex items-center gap-3 flex-wrap">
                <Badge variant="outline">v{m.version_no}</Badge>
                <StatusPill status={m.status} />
                <ConfidenceBadge value={m.confidence} />
                <span className="text-sm text-muted-foreground">
                  {m.fields.length} fields · {m.conditions.length} conditions · {m.blocks.length} blocks
                </span>
                <ChevronRight className="h-4 w-4 ml-auto text-muted-foreground" />
              </button>
            ))}
          </div>
        </div>
      )}
    </Panel>
  );
}

/* ------------------------------------------------------------- step 2 */
const SPAN_TONE: Record<string, string> = {
  blue: "bg-[color-mix(in_oklab,var(--color-token-source)_18%,transparent)] text-[var(--color-token-source)] border-b border-[var(--color-token-source)]",
  red: "bg-[color-mix(in_oklab,var(--color-token-prompt)_18%,transparent)] text-[var(--color-token-prompt)] border-b border-[var(--color-token-prompt)]",
};

function ReviewStep({ manifest, onManifestChange, onNext }: {
  manifest: any; onManifestChange: (m: any) => void; onNext: () => void;
}) {
  const [preview, setPreview] = useState<ManifestPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [conditions, setConditions] = useState<any[]>(manifest.conditions);

  useEffect(() => {
    setConditions(manifest.conditions);
    api.manifestPreview(manifest.id).then(setPreview).catch((e: any) => setError(e?.message ?? String(e)));
  }, [manifest.id]);

  const save = async () => {
    setSaving(true);
    try {
      const updated = await api.patchManifest(manifest.id, { conditions });
      onManifestChange(updated);
      toast.success("Corrections saved");
    } catch (e: any) {
      toast.error("Could not save", { description: e?.message ?? String(e) });
    } finally {
      setSaving(false);
    }
  };

  const approve = async () => {
    try {
      await api.approveManifest(manifest.id);
      onManifestChange({ ...manifest, status: "approved" });
      toast.success("Manifest approved", { description: "It's now immutable — recompile to make further changes." });
      onNext();
    } catch (e: any) {
      toast.error("Could not approve", { description: e?.message ?? String(e) });
    }
  };

  const summary = manifest.prescan_summary ?? {};
  const notes: string[] = summary.notes ?? [];
  const agentLog: any[] = summary.agent_log ?? [];

  return (
    <Panel
      title="Review what the compiler understood"
      description="Highlighted exactly as the compiler saw it — blue is a value it will fill, red is an instruction it will delete. Confirming this is quicker and safer than reading the manifest JSON."
      actions={
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={save} disabled={saving || manifest.status === "approved"}>
            {saving && <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />} Save corrections
          </Button>
          <Button size="sm" onClick={approve} disabled={manifest.status === "approved"} className="bg-gradient-brand text-white">
            <ShieldCheck className="h-4 w-4 mr-1.5" />
            {manifest.status === "approved" ? "Approved" : "Approve"}
          </Button>
        </div>
      }
    >
      <div className="grid grid-cols-2 md:grid-cols-6 gap-3 mb-6">
        {[
          ["Paragraphs", summary.paragraph_count],
          ["Blue spans", summary.blue_spans],
          ["Red spans", summary.red_spans],
          ["Merge fields", summary.mergefields],
          ["Conditions", manifest.conditions.length],
          ["Blocks", manifest.blocks.length],
        ].map(([label, value]) => (
          <div key={String(label)} className="rounded-lg border border-border bg-background/40 p-3">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
            <div className="text-xl font-semibold tabular-nums">{value ?? 0}</div>
          </div>
        ))}
      </div>

      {notes.length > 0 && (
        <div className="rounded-lg border border-info/30 bg-info/10 p-3 mb-6 text-sm space-y-1">
          {notes.map((n, i) => <div key={i}>· {n}</div>)}
        </div>
      )}

      {agentLog.length > 0 && (
        <div className="rounded-lg border border-border bg-background/40 p-3 mb-6">
          <div className="text-xs uppercase tracking-wider text-muted-foreground mb-2">Self-verification log</div>
          {agentLog.map((it: any) => (
            <div key={it.number} className="text-sm flex items-center gap-2">
              <Badge variant="outline" className="text-[10px]">round {it.number}</Badge>
              <span className={it.qa_passed ? "text-success" : "text-warning"}>{it.qa_passed ? "passed" : "failed"}</span>
              <span className="text-muted-foreground">{it.action}</span>
            </div>
          ))}
        </div>
      )}

      <div className="grid lg:grid-cols-[1fr_320px] gap-5">
        <div className="rounded-lg border border-border bg-background/40 max-h-[60vh] overflow-auto">
          {error && <div className="p-4 text-sm text-destructive">{error}</div>}
          {!preview && !error && <div className="p-4 text-sm text-muted-foreground">Loading the template…</div>}
          {preview?.paragraphs.map((p) => (
            <div
              key={p.index}
              className={cn(
                "px-4 py-1.5 text-sm leading-relaxed flex gap-3",
                p.is_condition_marker && "bg-warning/5 border-l-2 border-warning",
                p.block_ids.length > 0 && !p.is_condition_marker && "border-l-2 border-brand/40",
              )}
            >
              <span className="text-[10px] font-mono text-muted-foreground/60 w-8 shrink-0 pt-0.5">{p.index}</span>
              <span className="min-w-0">
                {p.spans.length === 0 && <span className="text-muted-foreground/40">·</span>}
                {p.spans.map((s) => (
                  <span
                    key={s.span_index}
                    title={s.field_id ? `field: ${s.field_id}` : s.is_instruction ? "instruction — will be deleted" : undefined}
                    className={cn(
                      "rounded-sm px-0.5",
                      SPAN_TONE[s.color],
                      s.is_instruction && "line-through opacity-70",
                    )}
                  >
                    {s.text}
                  </span>
                ))}
                {p.block_ids.length > 0 && (
                  <span className="ml-2 text-[10px] font-mono text-brand/70">{p.block_ids.join(" ")}</span>
                )}
              </span>
            </div>
          ))}
        </div>

        <div className="space-y-4">
          <div>
            <div className="text-xs uppercase tracking-wider text-muted-foreground mb-2">Conditions</div>
            <div className="space-y-2">
              {conditions.map((c, i) => (
                <div key={c.id} className="rounded-lg border border-border bg-background/40 p-3">
                  <div className="text-[11px] font-mono text-muted-foreground truncate">{c.id}</div>
                  <Input
                    value={c.expression}
                    disabled={manifest.status === "approved"}
                    onChange={(e) => setConditions(conditions.map((x, j) => (j === i ? { ...x, expression: e.target.value } : x)))}
                    className="mt-1.5 font-mono text-xs"
                  />
                  <div className="text-[11px] text-muted-foreground mt-1.5">keeps {c.keeps_blocks?.length ?? 0} block(s)</div>
                </div>
              ))}
              {conditions.length === 0 && (
                <p className="text-sm text-muted-foreground">
                  No conditional sections found. If this template does have them, recompile with self-verification.
                </p>
              )}
            </div>
          </div>

          <div>
            <div className="text-xs uppercase tracking-wider text-muted-foreground mb-2">Block boundaries</div>
            <div className="rounded-lg border border-border divide-y divide-border text-xs">
              {manifest.blocks.map((b: any) => (
                <div key={b.id} className="p-2.5 flex items-center justify-between gap-2">
                  <span className="font-mono truncate">{b.id}</span>
                  <span className="text-muted-foreground shrink-0">
                    ¶{b.start_paragraph}–{b.end_paragraph}
                    <Badge variant="outline" className="ml-1.5 text-[9px]">{b.boundary_method}</Badge>
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </Panel>
  );
}

/* ------------------------------------------------------------- step 3 */
function BindStep({ projectId, manifest, onNext }: { projectId: string; manifest: any; onNext: () => void }) {
  const [sources, setSources] = useState<any[]>([]);
  const [sourceId, setSourceId] = useState("");
  const [data, setData] = useState<Awaited<ReturnType<typeof api.bindingSuggestions>> | null>(null);
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api.listSources(projectId).then((r) => {
      const tabular = r.items.filter((s: any) => ["xlsx", "csv"].includes(s.file_type));
      setSources(tabular);
      if (tabular[0]) setSourceId(tabular[0].id);
    });
  }, [projectId]);

  const versionId = sources.find((s) => s.id === sourceId)?.current_version_id;

  useEffect(() => {
    if (!versionId) return;
    setLoading(true);
    api.bindingSuggestions(manifest.id, versionId)
      .then((r) => {
        setData(r);
        setBindings(Object.fromEntries(r.suggestions.filter((s) => s.column).map((s) => [s.field_id, s.column!])));
      })
      .catch((e: any) => toast.error("Could not read that source", { description: e?.message ?? String(e) }))
      .finally(() => setLoading(false));
  }, [manifest.id, versionId]);

  const save = async () => {
    if (!versionId) return;
    try {
      await api.saveBinding(manifest.id, { source_version_id: versionId, field_bindings: bindings });
      toast.success("Binding saved", { description: "Confirmed mappings are remembered for the next template." });
      onNext();
    } catch (e: any) {
      toast.error("Could not save the binding", { description: e?.message ?? String(e) });
    }
  };

  const grouped = useMemo(() => {
    const list = data?.suggestions ?? [];
    return {
      conditions: list.filter((s) => s.origin === "condition"),
      fields: list.filter((s) => s.origin !== "condition"),
    };
  }, [data]);

  return (
    <Panel
      title="Bind columns to fields"
      description="Everything that must have a value before a document can be produced. Confirmed mappings are written back to the org's field dictionary, so the next template with the same columns binds itself."
      actions={<Button size="sm" onClick={save} disabled={!versionId} className="bg-gradient-brand text-white">Save &amp; continue</Button>}
    >
      <div className="max-w-sm mb-6">
        <div className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">Source file</div>
        {sources.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No spreadsheet uploaded yet. Add a .xlsx or .csv in the project's Sources stage first.
          </p>
        ) : (
          <Select value={sourceId} onValueChange={setSourceId}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              {sources.map((s) => <SelectItem key={s.id} value={s.id}>{s.name}</SelectItem>)}
            </SelectContent>
          </Select>
        )}
      </div>

      {loading && <div className="text-sm text-muted-foreground flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin" /> Reading columns…</div>}

      {data && (
        <>
          <div className="flex items-center gap-3 mb-4 text-sm flex-wrap">
            <Badge variant="outline">{data.row_count} rows → {data.row_count} documents</Badge>
            <span className="text-muted-foreground">
              {Object.keys(bindings).length} of {data.suggestions.length} bound
            </span>
            {data.unused_columns.length > 0 && (
              <span className="text-muted-foreground">· unused: {data.unused_columns.join(", ")}</span>
            )}
          </div>

          {grouped.conditions.length > 0 && (
            <div className="mb-6">
              <div className="text-xs uppercase tracking-wider text-muted-foreground mb-2">
                Condition variables
              </div>
              <p className="text-xs text-muted-foreground mb-2 max-w-2xl">
                These decide which sections survive. They have no placeholder anywhere in the document, so
                they're easy to miss — and if one is unbound, every section that depends on it is dropped.
              </p>
              <BindingRows rows={grouped.conditions} columns={data.columns} bindings={bindings} onChange={setBindings} />
            </div>
          )}

          <div className="text-xs uppercase tracking-wider text-muted-foreground mb-2">Fields</div>
          <BindingRows rows={grouped.fields} columns={data.columns} bindings={bindings} onChange={setBindings} />
        </>
      )}
    </Panel>
  );
}

const METHOD_TONE: Record<string, string> = {
  mergefield: "text-success border-success/40 bg-success/10",
  dictionary: "text-brand border-brand/40 bg-brand/10",
  exact_slug: "text-success border-success/40 bg-success/10",
  fuzzy: "text-warning border-warning/40 bg-warning/10",
  llm: "text-purple border-purple/40 bg-purple/10",
  unmatched: "text-muted-foreground border-border bg-muted",
};

function BindingRows({ rows, columns, bindings, onChange }: {
  rows: BindingSuggestion[]; columns: string[];
  bindings: Record<string, string>; onChange: (b: Record<string, string>) => void;
}) {
  const set = useCallback((fieldId: string, column: string) => {
    const next = { ...bindings };
    if (column === "__none__") delete next[fieldId];
    else next[fieldId] = column;
    onChange(next);
  }, [bindings, onChange]);

  return (
    <div className="rounded-lg border border-border divide-y divide-border">
      {rows.map((s) => (
        <div key={s.field_id} className="p-3 grid md:grid-cols-[1.2fr_auto_1.2fr_auto] items-center gap-3">
          <div className="min-w-0">
            <code className="text-sm font-mono text-brand break-all">{s.field_id}</code>
            <div className="text-[11px] text-muted-foreground">{s.type}{s.origin === "condition" && " · condition variable"}</div>
          </div>
          <ChevronRight className="h-4 w-4 text-muted-foreground hidden md:block" />
          <Select value={bindings[s.field_id] ?? "__none__"} onValueChange={(v) => set(s.field_id, v)}>
            <SelectTrigger><SelectValue placeholder="Not bound" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__none__">— not bound —</SelectItem>
              {columns.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
            </SelectContent>
          </Select>
          <div className="text-right">
            <Badge variant="outline" className={cn("text-[10px]", METHOD_TONE[s.method])} title={s.rationale}>
              {s.method.replace("_", " ")}
            </Badge>
            {s.sample_value && <div className="text-[11px] text-muted-foreground mt-1 truncate max-w-[160px]">e.g. {s.sample_value}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------- step 4 */
function GenerateStep({ projectId, manifest }: { projectId: string; manifest: any }) {
  const [sources, setSources] = useState<any[]>([]);
  const [sourceId, setSourceId] = useState("");
  const [records, setRecords] = useState<any[]>([]);
  const [columns, setColumns] = useState<string[]>([]);
  const [previewing, setPreviewing] = useState<number | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<any | null>(null);

  useEffect(() => {
    api.listSources(projectId).then((r) => {
      const tabular = r.items.filter((s: any) => ["xlsx", "csv"].includes(s.file_type));
      setSources(tabular);
      if (tabular[0]) setSourceId(tabular[0].id);
    });
  }, [projectId]);

  const versionId = sources.find((s) => s.id === sourceId)?.current_version_id;

  useEffect(() => {
    if (!versionId) return;
    api.sourceRecords(versionId, 25).then((r) => { setRecords(r.records); setColumns(r.columns); });
  }, [versionId]);

  // Poll while the batch runs. The job row carries real per-row progress now,
  // rather than being written already-finished.
  useEffect(() => {
    if (!jobId) return;
    let alive = true;
    const tick = async () => {
      try {
        const next = await api.getJob(jobId);
        if (!alive) return;
        setJob(next);
        if (!["completed", "completed_with_errors", "failed"].includes(next.status)) setTimeout(tick, 1200);
      } catch { /* keep the last known state */ }
    };
    tick();
    return () => { alive = false; };
  }, [jobId]);

  const previewRow = async (rowIndex: number) => {
    if (!versionId) return;
    setPreviewing(rowIndex);
    try {
      const r = await api.previewRow(manifest.id, { source_version_id: versionId, row_index: rowIndex });
      toast[r.qa_passed ? "success" : "warning"](
        `Row ${rowIndex}: ${r.status}`,
        { description: r.qa_notes?.length ? r.qa_notes.join("; ") : "No leftover placeholders or instructions." },
      );
    } catch (e: any) {
      toast.error("Preview failed", { description: e?.message ?? String(e) });
    } finally {
      setPreviewing(null);
    }
  };

  const run = async () => {
    if (!versionId) return;
    try {
      const r = await api.generateBatch(manifest.id, { source_version_id: versionId, language: "en" });
      setJobId(r.job_id);
      toast.success("Batch started");
    } catch (e: any) {
      toast.error("Could not start the batch", { description: e?.message ?? String(e) });
    }
  };

  const download = async () => {
    if (!jobId) return;
    try {
      const url = await api.batchZipUrl(jobId);
      const a = document.createElement("a");
      a.href = url; a.download = "documents.zip"; a.click();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      toast.error("Could not download", { description: e?.message ?? String(e) });
    }
  };

  const progress = job?.progress ?? {};
  const done = progress.rows_done ?? 0;
  const total = progress.rows_total ?? records.length;
  const finished = ["completed", "completed_with_errors"].includes(job?.status);

  return (
    <Panel
      title="Generate"
      description="Preview a single row first — what you see is exactly what the batch produces. One row becomes one document."
      actions={
        <div className="flex gap-2">
          {finished && <Button size="sm" variant="outline" onClick={download}><Download className="h-4 w-4 mr-1.5" /> Download ZIP</Button>}
          <Button
            size="sm"
            onClick={run}
            disabled={!versionId || manifest.status !== "approved" || (!!jobId && !finished)}
            className="bg-gradient-brand text-white"
          >
            <Play className="h-4 w-4 mr-1.5" /> Generate {total || ""} document{total === 1 ? "" : "s"}
          </Button>
        </div>
      }
    >
      {manifest.status !== "approved" && (
        <div className="rounded-lg border border-warning/30 bg-warning/10 p-3 mb-5 text-sm">
          This manifest isn't approved yet. Approve it in the Review step before generating.
        </div>
      )}

      {jobId && (
        <div className="rounded-lg border border-border bg-background/40 p-4 mb-5">
          <div className="flex items-center justify-between text-sm mb-2">
            <span className="font-medium">{job?.status ?? "queued"}</span>
            <span className="tabular-nums text-muted-foreground">{done}/{total}</span>
          </div>
          <div className="h-2 rounded-full bg-muted overflow-hidden">
            <div className="h-full bg-gradient-brand transition-all" style={{ width: `${total ? (done / total) * 100 : 0}%` }} />
          </div>
          <div className="flex gap-4 mt-2 text-xs text-muted-foreground">
            <span>{progress.generated ?? 0} generated</span>
            <span>{progress.pending_review ?? 0} need review</span>
            <span>{progress.failed ?? 0} failed</span>
          </div>
        </div>
      )}

      <div className="rounded-lg border border-border overflow-hidden">
        <div className="overflow-x-auto max-h-[45vh]">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 sticky top-0">
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="px-3 py-2 font-medium">Row</th>
                {columns.slice(0, 5).map((c) => <th key={c} className="px-3 py-2 font-medium whitespace-nowrap">{c}</th>)}
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {records.map((r) => {
                const rowResult = (progress.rows ?? []).find((x: any) => x.row_index === r._row_index);
                return (
                  <tr key={r._row_index} className="hover:bg-accent/30">
                    <td className="px-3 py-2 font-mono text-xs text-muted-foreground">{r._row_index}</td>
                    {columns.slice(0, 5).map((c) => (
                      <td key={c} className="px-3 py-2 whitespace-nowrap max-w-[180px] truncate">{r[c]}</td>
                    ))}
                    <td className="px-3 py-2 text-right whitespace-nowrap">
                      {rowResult ? (
                        <Badge variant="outline" className={cn(
                          "text-[10px]",
                          rowResult.status === "generated" ? "text-success border-success/40 bg-success/10"
                            : rowResult.status === "pending_review" ? "text-warning border-warning/40 bg-warning/10"
                            : "text-destructive border-destructive/40 bg-destructive/10",
                        )}>{rowResult.status.replace("_", " ")}</Badge>
                      ) : (
                        <button
                          onClick={() => previewRow(r._row_index)}
                          disabled={previewing !== null}
                          className="text-xs text-brand hover:underline inline-flex items-center gap-1 disabled:opacity-50"
                        >
                          {previewing === r._row_index ? <Loader2 className="h-3 w-3 animate-spin" /> : <Table2 className="h-3 w-3" />}
                          Preview
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </Panel>
  );
}
