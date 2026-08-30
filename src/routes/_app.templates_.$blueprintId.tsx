/**
 * The Template Studio: the document, and what the engine understood about it.
 *
 * Deliberately not a rich-text editor. The whole pipeline addresses text by
 * `(paragraph_index, span_index)` and tells a placeholder from a static run by
 * its colour, and a rich-text model preserves neither -- round-tripping a
 * template through HTML is exactly the defect the backend's `text_edit` module
 * was written to close. So this renders paragraphs and the runs inside them, and
 * an edit changes one run.
 *
 * Which turns out to be the right shape for the job anyway. The unit a person
 * wants to change here is "this placeholder" or "this instruction", not "these
 * three words spanning two runs", and the unit the engine fills is the run.
 */

import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, ArrowLeft, CheckCircle2, Download, FileText, History, Info,
  Loader2, MessageSquare, RotateCcw, Save, Send, ShieldCheck, X,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import type {
  Blueprint, BlueprintBlock, BlueprintBody, BlueprintParagraph, BlueprintSegment,
  LintFinding, LintReport, SegmentRole,
} from "@/lib/types";

// `templates_` rather than `templates`: the trailing underscore is TanStack
// Router's way of saying "do not nest me inside that route". Without it this
// registers as a child of the templates list page, which renders no `<Outlet />`
// -- so navigating here showed the list again and the studio never appeared,
// with no error anywhere to say why. The same convention the project routes
// already use for `$id_.edit` and `$id_.studio`.
export const Route = createFileRoute("/_app/templates_/$blueprintId")({
  component: StudioPage,
});

/** The same three roles the pre-scanner classifies runs into, and their colours. */
const ROLE_STYLE: Record<SegmentRole, { className: string; label: string }> = {
  static: { className: "text-foreground", label: "Static text" },
  placeholder: { className: "text-[var(--color-token-source)] font-medium", label: "Placeholder" },
  instruction: { className: "text-[var(--color-token-prompt)]", label: "Author instruction" },
  mergefield: { className: "text-[var(--color-token-repeat)] font-mono text-[0.85em]", label: "Merge field" },
  hyperlink: { className: "text-primary underline underline-offset-2", label: "Hyperlink" },
};

const SEVERITY_STYLE: Record<string, string> = {
  blocking: "border-destructive/40 bg-destructive/10 text-destructive",
  warning: "border-amber-500/40 bg-amber-500/10 text-amber-500",
  advisory: "border-border bg-muted/40 text-muted-foreground",
};

/** Paragraphs in the order the backend indexes them: document order, into tables. */
function walkParagraphs(body: BlueprintBody | undefined) {
  const out: { index: number; block: BlueprintParagraph; inTable: boolean; path: number[] }[] = [];
  const walk = (blocks: BlueprintBlock[], inTable: boolean, path: number[]) => {
    blocks.forEach((block, i) => {
      if (block.kind === "paragraph") {
        out.push({ index: out.length, block, inTable, path: [...path, i] });
      } else {
        block.rows.forEach((row, r) =>
          row.forEach((cell, c) => walk(cell, true, [...path, i, r, c])));
      }
    });
  };
  walk(body?.blocks ?? [], false, []);
  return out;
}

/** Replace one segment, without mutating anything the caller still holds. */
function withSegment(
  body: BlueprintBody, path: number[], segmentIndex: number,
  change: (segment: BlueprintSegment) => BlueprintSegment,
): BlueprintBody {
  const replaceIn = (blocks: BlueprintBlock[], remaining: number[]): BlueprintBlock[] => {
    const [head, ...rest] = remaining;
    return blocks.map((block, i) => {
      if (i !== head) return block;
      if (block.kind === "paragraph") {
        return {
          ...block,
          segments: block.segments.map((s, si) => (si === segmentIndex ? change(s) : s)),
        };
      }
      const [rowIndex, cellIndex, ...deeper] = rest;
      return {
        ...block,
        rows: block.rows.map((row, r) => r !== rowIndex ? row : row.map((cell, c) =>
          c !== cellIndex ? cell : replaceIn(cell, deeper))),
      };
    });
  };
  return { ...body, blocks: replaceIn(body.blocks, path) };
}

function StudioPage() {
  const { blueprintId } = Route.useParams();
  const navigate = useNavigate();

  const [blueprint, setBlueprint] = useState<Blueprint | null>(null);
  const [body, setBody] = useState<BlueprintBody | null>(null);
  const [dirty, setDirty] = useState(false);
  const [selected, setSelected] = useState<{ path: number[]; segmentIndex: number } | null>(null);
  const [lint, setLint] = useState<LintReport | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [versions, setVersions] = useState<any[]>([]);
  const [showVersions, setShowVersions] = useState(false);

  const load = useCallback(async () => {
    const bp = await api.getBlueprint(blueprintId);
    setBlueprint(bp);
    setBody(bp.version?.body ?? null);
    setDirty(false);
    setSelected(null);
    api.lintBlueprint(blueprintId).then(setLint).catch(() => setLint(null));
    api.listBlueprintVersions(blueprintId).then((r) => setVersions(r.items)).catch(() => {});
  }, [blueprintId]);

  useEffect(() => {
    load().catch((e: any) =>
      toast.error("Could not open this template", { description: e?.message ?? String(e) }));
  }, [load]);

  const paragraphs = useMemo(() => walkParagraphs(body ?? undefined), [body]);

  const selectedSegment: BlueprintSegment | null = useMemo(() => {
    if (!selected || !body) return null;
    const entry = paragraphs.find((p) => p.path.join(".") === selected.path.join("."));
    return entry?.block.segments[selected.segmentIndex] ?? null;
  }, [selected, paragraphs, body]);

  const findingsByParagraph = useMemo(() => {
    const map = new Map<number, LintFinding[]>();
    for (const f of lint?.findings ?? []) {
      if (f.paragraph_index == null) continue;
      map.set(f.paragraph_index, [...(map.get(f.paragraph_index) ?? []), f]);
    }
    return map;
  }, [lint]);

  const update = (change: (segment: BlueprintSegment) => BlueprintSegment) => {
    if (!selected || !body) return;
    setBody(withSegment(body, selected.path, selected.segmentIndex, change));
    setDirty(true);
  };

  const save = async () => {
    if (!body || !blueprint) return;
    setBusy("save");
    try {
      await api.saveBlueprint(blueprintId, {
        body,
        change_summary: "Edited in the template studio.",
        expected_version_no: blueprint.version_no ?? undefined,
      });
      toast.success("Saved");
      await load();
    } catch (e: any) {
      toast.error(
        e?.code === "BLUEPRINT_VERSION_CONFLICT" ? "Somebody else saved first" : "Could not save",
        { description: e?.message ?? String(e), duration: 8000 });
    } finally { setBusy(null); }
  };

  const download = async () => {
    setBusy("download");
    try {
      const url = await api.blueprintDocxUrl(blueprintId);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${blueprint?.name ?? "template"}.docx`.replace(/\.docx\.docx$/, ".docx");
      a.click();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      toast.error("Could not download", { description: e?.message ?? String(e) });
    } finally { setBusy(null); }
  };

  const publish = async () => {
    setBusy("publish");
    try {
      const result = await api.publishBlueprint(blueprintId);
      setLint(result.lint);
      toast.success("Published", {
        description: "The template and the manifest that fills it are both written.",
      });
      await load();
    } catch (e: any) {
      const report = e?.details?.lint as LintReport | undefined;
      if (report) setLint(report);
      toast.error("Not ready to publish", {
        description: e?.message ?? String(e), duration: 10000,
      });
    } finally { setBusy(null); }
  };

  if (!blueprint || !body) {
    return (
      <div className="flex h-[60vh] items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Opening the template…
      </div>
    );
  }

  const blocking = lint?.findings.filter((f) => f.severity === "blocking") ?? [];

  return (
    <div className="mx-auto max-w-[1500px] space-y-4 p-6 lg:p-8">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link to="/templates" className="mb-1 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
            <ArrowLeft className="h-3 w-3" /> Templates
          </Link>
          <h1 className="truncate text-2xl font-semibold tracking-tight">{blueprint.name}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Badge variant="outline">v{blueprint.version_no}</Badge>
            <Badge variant={blueprint.status === "published" ? "default" : "secondary"}>
              {blueprint.status}
            </Badge>
            <span>{paragraphs.length} paragraphs</span>
            {blueprint.kind === "legacy" && (
              <span className="inline-flex items-center gap-1">
                <Info className="h-3 w-3" />
                read from an uploaded file, which is kept so this can be put back
              </span>
            )}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => setShowVersions((s) => !s)} className="gap-1.5">
            <History className="h-3.5 w-3.5" /> Versions
          </Button>
          <Button variant="outline" size="sm" onClick={download} disabled={busy != null} className="gap-1.5">
            {busy === "download" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
            Download .docx
          </Button>
          <Button size="sm" onClick={save} disabled={!dirty || busy != null} className="gap-1.5">
            {busy === "save" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
            {dirty ? "Save" : "Saved"}
          </Button>
          <Button size="sm" variant={blocking.length ? "outline" : "default"} onClick={publish}
                  disabled={busy != null || dirty} className="gap-1.5">
            {busy === "publish" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ShieldCheck className="h-3.5 w-3.5" />}
            Publish
          </Button>
        </div>
      </div>

      {showVersions && (
        <div className="rounded-xl border border-border bg-card p-4">
          <p className="mb-2 text-xs uppercase tracking-wider text-muted-foreground">History</p>
          <div className="divide-y divide-border">
            {versions.map((v) => (
              <div key={v.id} className="flex items-center justify-between gap-3 py-2 text-sm">
                <div className="min-w-0">
                  <span className="font-medium">v{v.version_no}</span>
                  <span className="ml-2 text-muted-foreground">{v.change_summary}</span>
                </div>
                {v.version_no !== blueprint.version_no && (
                  <Button variant="ghost" size="sm" className="gap-1.5"
                          onClick={async () => {
                            await api.revertBlueprint(blueprintId, v.version_no);
                            toast.success(`Forked a new version from v${v.version_no}`);
                            await load();
                          }}>
                    <RotateCcw className="h-3.5 w-3.5" /> Put back to this
                  </Button>
                )}
              </div>
            ))}
          </div>
          <p className="mt-2 text-[11px] text-muted-foreground">
            Putting back forks a new version rather than erasing the ones after it — the history is
            a record of the work, not of the current opinion.
          </p>
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_360px]">
        {/* The document */}
        <div className="max-h-[calc(100vh-260px)] overflow-auto rounded-xl border border-border bg-card p-6">
          {paragraphs.map(({ index, block, inTable, path }) => {
            const found = findingsByParagraph.get(index) ?? [];
            return (
              <div key={index}
                   className={cn("group relative -mx-2 rounded px-2 py-0.5",
                                 inTable && "border-l-2 border-border/70 pl-3",
                                 found.some((f) => f.severity === "blocking") && "bg-destructive/5")}>
                <span className="pointer-events-none absolute -left-9 top-1 hidden text-[10px] tabular-nums text-muted-foreground group-hover:block">
                  {index}
                </span>
                <p className={cn("min-h-[1.4em] leading-relaxed",
                                 block.style?.startsWith("Heading") && "mt-4 text-lg font-semibold")}>
                  {block.segments.map((segment, si) => {
                    const isSelected = selected?.path.join(".") === path.join(".")
                      && selected?.segmentIndex === si;
                    const style = ROLE_STYLE[segment.role];
                    const hidden = segment.emit === false;
                    return (
                      <button
                        key={si}
                        onClick={() => setSelected({ path, segmentIndex: si })}
                        className={cn(
                          "rounded px-0.5 text-left align-baseline transition-colors hover:bg-accent",
                          style.className,
                          hidden && "line-through opacity-40",
                          isSelected && "bg-primary/20 ring-1 ring-primary",
                        )}
                        title={hidden ? `${style.label} — removed from the published template` : style.label}
                      >
                        {segment.role === "mergefield" ? `«${segment.code}»` : segment.text || " "}
                      </button>
                    );
                  })}
                </p>
              </div>
            );
          })}
        </div>

        {/* Inspector + findings */}
        <div className="space-y-4">
          <div className="rounded-xl border border-border bg-card p-4">
            <p className="mb-3 text-xs uppercase tracking-wider text-muted-foreground">Selected run</p>
            {!selectedSegment ? (
              <p className="text-sm text-muted-foreground">
                Click any run in the document to see what the engine thinks it is, and change it.
              </p>
            ) : (
              <div className="space-y-3">
                <div className="flex flex-wrap gap-1.5">
                  {(["static", "placeholder", "instruction"] as SegmentRole[]).map((role) => (
                    <button
                      key={role}
                      disabled={selectedSegment.role === "mergefield" || selectedSegment.role === "hyperlink"}
                      onClick={() => update((s) => ({ ...s, role }))}
                      className={cn(
                        "rounded-full border px-2.5 py-1 text-xs font-medium transition-colors disabled:opacity-40",
                        selectedSegment.role === role
                          ? "border-primary bg-primary text-primary-foreground"
                          : "border-border hover:bg-accent",
                      )}
                    >
                      {ROLE_STYLE[role].label}
                    </button>
                  ))}
                </div>

                {selectedSegment.role === "mergefield" ? (
                  <Input value={selectedSegment.code ?? ""} readOnly className="font-mono text-xs" />
                ) : (
                  <Textarea
                    value={selectedSegment.text}
                    onChange={(e) => update((s) => ({ ...s, text: e.target.value }))}
                    rows={3}
                    className="text-sm"
                  />
                )}

                {selectedSegment.role === "instruction" && (
                  <label className="flex items-start gap-2 text-xs text-muted-foreground">
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={selectedSegment.emit === false}
                      onChange={(e) => update((s) => ({ ...s, emit: e.target.checked ? false : undefined }))}
                    />
                    <span>
                      Leave this out of the published template. The compile marks the instructions
                      it recognised; you decide whether each one was written for you or for the
                      reader.
                    </span>
                  </label>
                )}
              </div>
            )}
          </div>

          <CopilotPanel blueprintId={blueprintId} versionNo={blueprint.version_no ?? 1}
                        onApplied={load} />

          <div className="rounded-xl border border-border bg-card p-4">
            <div className="mb-3 flex items-center justify-between">
              <p className="text-xs uppercase tracking-wider text-muted-foreground">Will this work?</p>
              {lint && (lint.can_publish
                ? <span className="inline-flex items-center gap-1 text-xs text-emerald-500">
                    <CheckCircle2 className="h-3.5 w-3.5" /> ready
                  </span>
                : <span className="inline-flex items-center gap-1 text-xs text-destructive">
                    <AlertTriangle className="h-3.5 w-3.5" /> {lint.blocking} blocking
                  </span>)}
            </div>
            {!lint?.findings.length ? (
              <p className="text-sm text-muted-foreground">
                Nothing to flag. Every placeholder is claimed, every condition reads something, and
                every field has somewhere in the document to go.
              </p>
            ) : (
              <div className="max-h-[40vh] space-y-2 overflow-auto">
                {lint.findings.map((f, i) => (
                  <button
                    key={i}
                    onClick={() => {
                      const target = paragraphs.find((p) => p.index === f.paragraph_index);
                      if (target) setSelected({ path: target.path, segmentIndex: 0 });
                    }}
                    className={cn("w-full rounded-lg border p-2.5 text-left text-xs",
                                  SEVERITY_STYLE[f.severity] ?? SEVERITY_STYLE.advisory)}
                  >
                    <div className="mb-0.5 flex items-center gap-1.5 font-mono text-[10px] uppercase opacity-70">
                      {f.severity} · {f.code}
                      {f.paragraph_index != null && <span>· paragraph {f.paragraph_index}</span>}
                    </div>
                    <div className="leading-snug">{f.detail}</div>
                  </button>
                ))}
              </div>
            )}
          </div>

          {blueprint.status === "published" && (
            <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-4 text-xs">
              <p className="mb-1 flex items-center gap-1.5 font-medium text-emerald-500">
                <FileText className="h-3.5 w-3.5" /> Published
              </p>
              <p className="text-muted-foreground">
                A template version and the manifest that fills it are both written. Bind it to a
                spreadsheet from the project's Document Mapping tab to generate documents.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}


/**
 * Change the template by asking, or ask what it does.
 *
 * The model never writes the document. It proposes typed operations, the server
 * has already run them through the same guards a hand edit meets, and what is
 * shown here is what *would* happen -- including which suggestions were refused
 * and why. Nothing is saved until Apply.
 *
 * The mode is a switch rather than something inferred from the wording. Guessing
 * between "explain this condition" and "change this condition" is the kind of
 * guess that edits a legal template by accident.
 */
function CopilotPanel({ blueprintId, versionNo, onApplied }: {
  blueprintId: string; versionNo: number; onApplied: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"author" | "explain">("author");
  const [message, setMessage] = useState("");
  const [asking, setAsking] = useState(false);
  const [proposal, setProposal] = useState<any>(null);

  const ask = async () => {
    if (!message.trim()) return;
    setAsking(true);
    setProposal(null);
    try {
      setProposal(await api.blueprintCopilot(blueprintId, { message: message.trim(), mode }));
    } catch (e: any) {
      toast.error(
        e?.code === "LLM_NOT_CONFIGURED" ? "No language model is configured" : "Could not ask",
        { description: e?.message ?? String(e), duration: 8000 });
    } finally { setAsking(false); }
  };

  const apply = async () => {
    setAsking(true);
    try {
      await api.applyBlueprintOperations(blueprintId, {
        ops: proposal.operations,
        expected_version_no: versionNo,
        change_summary: message.trim().slice(0, 120),
      });
      toast.success("Applied");
      setProposal(null);
      setMessage("");
      await onApplied();
    } catch (e: any) {
      toast.error("Could not apply", { description: e?.message ?? String(e) });
    } finally { setAsking(false); }
  };

  if (!open) {
    return (
      <Button variant="outline" className="w-full gap-2" onClick={() => setOpen(true)}>
        <MessageSquare className="h-4 w-4" /> Ask about this template
      </Button>
    );
  }

  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs uppercase tracking-wider text-muted-foreground">Co-pilot</p>
        <button onClick={() => setOpen(false)} className="text-muted-foreground hover:text-foreground">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="mb-2 flex gap-1.5">
        {(["author", "explain"] as const).map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            className={cn("rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
                          mode === m ? "border-primary bg-primary text-primary-foreground"
                                     : "border-border hover:bg-accent")}
          >
            {m === "author" ? "Change it" : "Explain it"}
          </button>
        ))}
      </div>

      <Textarea
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        rows={3}
        placeholder={mode === "author"
          ? "Rename the salary field to something the HR team would recognise"
          : "Why would a part-time colleague's letter lose the bonus paragraph?"}
        className="text-sm"
      />
      <Button size="sm" onClick={ask} disabled={asking || !message.trim()} className="mt-2 w-full gap-1.5">
        {asking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
        {mode === "author" ? "Propose changes" : "Ask"}
      </Button>

      {proposal?.mode === "explain" && (
        <div className="mt-3 space-y-2">
          {proposal.answers?.map((a: any, i: number) => (
            <div key={i} className="rounded-lg border border-border bg-muted/40 p-2.5 text-xs">
              <p className="leading-snug">{a.answer}</p>
              {a.paragraph_indices?.length > 0 && (
                <p className="mt-1 text-[11px] text-muted-foreground">
                  paragraphs {a.paragraph_indices.join(", ")}
                </p>
              )}
            </div>
          ))}
          {!proposal.answers?.length && (
            <p className="text-xs text-muted-foreground">
              Nothing in the template or its lineage answers that.
            </p>
          )}
        </div>
      )}

      {proposal?.mode === "author" && (
        <div className="mt-3 space-y-2">
          {proposal.operations?.length > 0 ? (
            <>
              <p className="text-xs text-muted-foreground">
                {proposal.operations.length} change{proposal.operations.length === 1 ? "" : "s"} would
                be made. Nothing is saved until you apply them.
              </p>
              {proposal.operations.map((op: any, i: number) => (
                <div key={i} className="rounded-lg border border-border bg-muted/40 p-2.5 text-xs">
                  <div className="font-mono text-[10px] uppercase opacity-70">{op.op}</div>
                  {op.reason && <div className="mt-0.5 leading-snug">{op.reason}</div>}
                </div>
              ))}
              <Button size="sm" onClick={apply} disabled={asking} className="w-full gap-1.5">
                <CheckCircle2 className="h-3.5 w-3.5" /> Apply {proposal.operations.length}
              </Button>
            </>
          ) : (
            <p className="text-xs text-muted-foreground">
              {proposal.verdict === "need_more_context"
                ? "Not enough to go on — it asked rather than guessed."
                : "No change was proposed."}
            </p>
          )}

          {proposal.questions?.map((q: string, i: number) => (
            <p key={i} className="text-xs italic text-muted-foreground">“{q}”</p>
          ))}

          {proposal.rejected?.length > 0 && (
            <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-2.5 text-xs text-amber-500">
              <p className="mb-1 font-medium">
                {proposal.rejected.length} suggestion{proposal.rejected.length === 1 ? "" : "s"} refused
              </p>
              {proposal.rejected.slice(0, 3).map((r: any, i: number) => (
                <p key={i} className="leading-snug opacity-90">{r.reason}</p>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
