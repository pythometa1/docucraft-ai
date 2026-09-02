/**
 * The template editor: the document, and what the engine understood about it.
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
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Download,
  History,
  Info,
  Sparkles,
  Loader2,
  MessageSquare,
  RotateCcw,
  Save,
  Send,
  Trash2,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { cn } from "@/lib/utils";
import type {
  Blueprint, BlueprintBlock, BlueprintBody, BlueprintParagraph, BlueprintSegment,
  LintFinding, LintReport, SegmentRole,
} from "@/lib/types";

// `templates_` rather than `templates`: the trailing underscore is TanStack
// Router's way of saying "do not nest me inside that route". Without it this
// registers as a child of the templates list page, which renders no `<Outlet />`
// -- so navigating here showed the list again and the editor never appeared,
// with no error anywhere to say why. The same convention the project route
// already uses for `$id_.edit`.
export const Route = createFileRoute("/_app/templates_/$blueprintId")({
  /** Where the user came from, when they came from a project.
   *
   *  Optional, because this screen is also reached from the flat Templates list
   *  and has to keep working with no origin at all. When it is present the Back
   *  link goes home instead of to a list the user was never on, and publishing
   *  returns them to the project rather than leaving them on the editor of a
   *  template they have finished with. */
  validateSearch: (search: Record<string, unknown>): { project?: string; template?: string } => {
    // Keys are omitted rather than set to undefined: a validator that always
    // returns both makes them *required* at every call site, and the three
    // existing links from the flat Templates list have no project to name.
    const out: { project?: string; template?: string } = {};
    if (typeof search.project === "string") out.project = search.project;
    if (typeof search.template === "string") out.template = search.template;
    return out;
  },
  component: TemplateEditorPage,
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

function TemplateEditorPage() {
  const { blueprintId } = Route.useParams();
  const { project: fromProject } = Route.useSearch();
  const navigate = useNavigate();

  const [blueprint, setBlueprint] = useState<Blueprint | null>(null);
  const [body, setBody] = useState<BlueprintBody | null>(null);
  const [dirty, setDirty] = useState(false);
  const [selected, setSelected] = useState<{ path: number[]; segmentIndex: number } | null>(null);
  const [lint, setLint] = useState<LintReport | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [versions, setVersions] = useState<any[]>([]);
  const [showVersions, setShowVersions] = useState(false);
  /** Asked before publishing when the lint panel has blocking findings, because
   *  re-reading skips the server's own gate on them. */
  const [confirmPublish, setConfirmPublish] = useState(false);
  /** The model could not read the edited template. Nothing was published, and
   *  the fallback the server's message names is offered from here. */
  const [readFailure, setReadFailure] = useState<string | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);

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
        change_summary: "Edited in the template editor.",
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

  /**
   * Write the edited template and have the compiler read it again.
   *
   * `recompile` is the default rather than a second button, because it is what
   * makes an edit take effect. The blueprint's own objects are a reading of the
   * document as it was *before* the edit; publishing from them writes a manifest
   * that describes the old wording, which is how you repair a placeholder and
   * watch nothing change. Re-reading means the manifest that ships is a reading
   * of the bytes that shipped.
   *
   * Two things follow from that and are handled below rather than hidden:
   *
   * `recompile` skips the publishability lint gate -- deliberately, on the
   * server: the objects are not what ships on this path, so refusing on them
   * would refuse a template that is now correct for a reading that is merely out
   * of date. Which means this button is not lint-gated, so a blocking finding is
   * confirmed here instead.
   *
   * And re-reading needs a language model, so it can fail with `TEMPLATE_NOT_READ`
   * when one is unreachable. The server's own message tells the reader to publish
   * without re-reading, so that route has to exist: `fallback` is it, and it *is*
   * lint-gated.
   */
  const publish = async (recompile = true) => {
    setBusy(recompile ? "publish" : "fallback");
    try {
      const result = await api.publishBlueprint(blueprintId, [], recompile);
      setLint(result.lint);
      setConfirmPublish(false);
      setReadFailure(null);

      // Compiling approves its own reading where it may, and publishing takes
      // the same path -- so in the ordinary case the new version is live the
      // moment this returns. Where it could not (a role without the capability,
      // or a legally binding template) the server says why, and repeating that
      // here is the difference between "published" and "published, and still not
      // the version your project generates from".
      const pending = (result as any)?.approval_blocked_reason as string | null | undefined;
      toast.success(
        recompile ? "Template regenerated — updated version published" : "Published",
        {
          description: pending
            ? `${pending} Until then, generation still uses the previous version.`
            : "The template and the manifest that fills it are both live.",
          duration: pending ? 10000 : 5000,
        },
      );

      // Back to where they came from. Finishing an edit is finishing with this
      // screen, and leaving the user on it after a successful publish is leaving
      // them to work out for themselves that it is over.
      if (fromProject) {
        navigate({ to: "/projects/$id", params: { id: fromProject } });
      } else {
        navigate({ to: "/templates" });
      }
      return;
    } catch (e: any) {
      const report = e?.details?.lint as LintReport | undefined;
      if (report) setLint(report);
      if (e?.code === "TEMPLATE_NOT_READ" || e?.code === "LLM_NOT_CONFIGURED") {
        // Nothing was published -- the server compiles before it writes, so the
        // template is exactly as it was. Offer the route its own message names.
        setReadFailure(e?.message ?? String(e));
        return;
      }
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
          {fromProject ? (
            <Link
              to="/projects/$id"
              params={{ id: fromProject }}
              className="mb-1 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            >
              <ArrowLeft className="h-3 w-3" /> Back to project
            </Link>
          ) : (
            <Link to="/templates" className="mb-1 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
              <ArrowLeft className="h-3 w-3" /> Templates
            </Link>
          )}
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
          {/* One action, where there were two.
              The old pair was "Publish" and, for legacy templates only,
              "Publish & re-read" -- and the first of them wrote a manifest
              describing the wording as it was before the edit. Re-reading is
              what makes an edit take effect, so it is not an option. */}
          <Button size="sm" className="gap-1.5"
                  onClick={() => (blocking.length ? setConfirmPublish(true) : publish(true))}
                  disabled={busy != null || dirty}
                  title="Write the edited template, have the compiler read it again, and publish the result">
            {busy === "publish" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
            Publish &amp; regenerate updated template
          </Button>
        </div>
      </div>

      {/* Re-reading skips the server's publishability gate on purpose -- the
          objects are not what ships -- so a blocking finding has to be confirmed
          here or it is not raised at all. */}
      <AlertDialog open={confirmPublish} onOpenChange={(o) => { if (busy == null) setConfirmPublish(o); }}>
        <AlertDialogContent className="border-border bg-surface">
          <AlertDialogHeader>
            <AlertDialogTitle>
              Publish with {blocking.length} unresolved {blocking.length === 1 ? "finding" : "findings"}?
            </AlertDialogTitle>
            <AlertDialogDescription className="whitespace-pre-line">
              {"The compiler reads the document again on the way out, so these may no longer apply — "
               + "they describe the reading this template is carrying now, not the one that will ship. "
               + "But nothing checks them again, so if they are real they will reach the letters.\n\n"
               + blocking.slice(0, 3).map((f) => f.detail).join("\n")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy != null}>Go back</AlertDialogCancel>
            <AlertDialogAction disabled={busy != null}
                               onClick={(e) => { e.preventDefault(); void publish(true); }}>
              {busy != null ? "Publishing…" : "Publish anyway"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* The compile runs before anything is written, so this means the template
          is exactly as it was. The server's own message says to publish without
          re-reading; that has to be reachable, and it is lint-gated. */}
      <AlertDialog open={readFailure != null} onOpenChange={(o) => { if (!o && busy == null) setReadFailure(null); }}>
        <AlertDialogContent className="border-border bg-surface">
          <AlertDialogHeader>
            <AlertDialogTitle>The edited template could not be read</AlertDialogTitle>
            <AlertDialogDescription className="whitespace-pre-line">
              {`${readFailure ?? ""}\n\nNothing was published — your template is exactly as it was. `
               + "You can publish without re-reading, which ships the reading this editor is carrying "
               + "rather than a fresh one. Every check runs on that path, so it refuses if the reading "
               + "no longer matches the document."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy != null}>Leave it</AlertDialogCancel>
            <AlertDialogAction disabled={busy != null}
                               onClick={(e) => { e.preventDefault(); void publish(false); }}>
              {busy === "fallback" ? "Publishing…" : "Publish without re-reading"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {showVersions && (
        <div className="rounded-xl surface-raised p-4">
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
        <div className="max-h-[calc(100vh-260px)] overflow-auto rounded-xl surface-raised p-6">
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
          <div className="rounded-xl surface-raised p-4">
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

          <div className="rounded-xl surface-raised p-4">
            <div className="mb-3 flex items-center justify-between">
              <p className="text-xs uppercase tracking-wider text-muted-foreground">Will this work?</p>
              {lint && (lint.can_publish
                ? <span
                    className="inline-flex items-center gap-1 text-xs text-emerald-500"
                    title="These checks read the blueprint. Publishing runs a stricter set against the document it writes, so it can still refuse."
                  >
                    {/* Not "ready": this pass writes no document, and the check
                        that asks an emitted file what is still wrong only runs
                        at publish. Saying ready here and refusing there is the
                        contradiction that made the refusal look like a bug. */}
                    <CheckCircle2 className="h-3.5 w-3.5" /> nothing flagged here
                  </span>
                : <span className="inline-flex items-center gap-1 text-xs text-destructive">
                    <AlertTriangle className="h-3.5 w-3.5" /> {lint.blocking} blocking
                  </span>)}
            </div>
            {/* Said where the blocking findings are actually read, because a
                refusal with no alternative reads as "this template cannot be
                published" -- which is false when the document is fine and only
                the reading is out of date. */}
            {blueprint.kind === "legacy" && (
              <div className="mb-3 rounded-lg border border-border bg-background/40 p-2.5 text-xs">
                <p className="text-muted-foreground">
                  These checks read the blueprint, not the finished file. Publishing has the
                  compiler read the document it writes and builds the manifest from <em>that</em>,
                  so a finding here describes the reading this editor is carrying rather than the
                  one that will ship — and one you have already fixed in the document may still be
                  listed. They are worth reading before you publish, not worth being stopped by.
                </p>
              </div>
            )}
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
                <CheckCircle2 className="h-3.5 w-3.5" /> Published
              </p>
              {/* This used to say the manifest was still a draft and point at a
                  screen to go and approve it on. Publishing now approves what it
                  writes, so the warning is no longer true -- and where it cannot
                  approve (a role without the capability, a legally binding
                  template) the publish toast says so at the moment it happens,
                  which is where that belongs. */}
              <p className="text-muted-foreground">
                The template and the manifest that fills it are both live. Documents generated from
                this project use this version.
              </p>
            </div>
          )}

          <DeleteTemplatePanel
            blueprintId={blueprintId}
            name={blueprint.name}
            open={deleteOpen}
            onOpenChange={setDeleteOpen}
            onGone={() => navigate(
              fromProject
                ? { to: "/projects/$id", params: { id: fromProject } }
                : { to: "/templates" },
            )}
          />
        </div>
      </div>
    </div>
  );
}


/**
 * Remove this template, under the chat that edits it.
 *
 * Two outcomes, and the difference is worth being honest about rather than
 * hiding behind one word. A template nothing has been published from is deleted:
 * it comes off the list and the retention sweep destroys the file later. One
 * that *has* been published is refused, because letters already sent name the
 * version they came from -- and for those the server offers an archive, which
 * takes it off the list and leaves everything generated from it alone.
 *
 * The refusal is the common case now, not the rare one: compiling approves its
 * own reading, so almost every template that has been read has an approved
 * manifest. So rather than showing the user a 409 and stopping, this asks the
 * question again with the honest answer attached.
 */
function DeleteTemplatePanel({ blueprintId, name, open, onOpenChange, onGone }: {
  blueprintId: string;
  name: string;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onGone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  /** Set when the server refused the delete because something was published
   *  from this template. Carries the reason it gave. */
  const [inUse, setInUse] = useState<string | null>(null);

  const run = async (archive: boolean) => {
    setBusy(true);
    try {
      if (archive) {
        await api.archiveBlueprint(blueprintId);
        toast.success("Template archived", {
          description: `${name} is off the list. Everything generated from it is unchanged.`,
        });
      } else {
        await api.deleteBlueprint(blueprintId);
        toast.success("Template deleted", { description: name });
      }
      onOpenChange(false);
      onGone();
    } catch (e: any) {
      if (!archive && e?.code === "BLUEPRINT_IN_USE") {
        // Not an error to report and stop on -- it is the answer to a question
        // the user has not been asked yet.
        setInUse(e?.message ?? "Something has been published from this template.");
        return;
      }
      toast.error(archive ? "Could not archive this template" : "Could not delete this template", {
        description: e?.message ?? String(e),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-xl border border-destructive/25 bg-destructive/5 p-4">
      <p className="mb-1 flex items-center gap-1.5 text-xs font-medium text-destructive">
        <Trash2 className="h-3.5 w-3.5" /> Remove this template
      </p>
      <p className="mb-3 text-xs text-muted-foreground">
        Takes it off the Templates list. Anything already generated from it is kept.
      </p>
      <Button variant="outline" size="sm"
              className="w-full gap-2 border-destructive/40 text-destructive hover:bg-destructive/10"
              onClick={() => { setInUse(null); onOpenChange(true); }}>
        <Trash2 className="h-3.5 w-3.5" /> Delete template
      </Button>

      <AlertDialog open={open} onOpenChange={(o) => { if (!busy) { onOpenChange(o); if (!o) setInUse(null); } }}>
        <AlertDialogContent className="border-border bg-surface">
          <AlertDialogHeader>
            <AlertDialogTitle>
              {inUse ? `Archive “${name}” instead?` : `Delete “${name}”?`}
            </AlertDialogTitle>
            <AlertDialogDescription className="whitespace-pre-line">
              {inUse
                ? `${inUse}\n\nArchiving takes it off the Templates list and changes nothing else: `
                  + "the template, its versions and every document generated from it stay exactly "
                  + "as they are. Documents already sent keep working."
                : "This removes the template from the list. Anything already compiled or generated "
                  + "from it is kept, and the file itself is destroyed later by the retention sweep, "
                  + "on the schedule your organisation set."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={busy}
              className={cn("bg-destructive text-destructive-foreground hover:bg-destructive/90")}
              // Radix closes on action click; the dialog stays up while the
              // request is in flight so the disabled state is visible, a second
              // click cannot fire it, and the refusal above can replace the
              // question in place rather than after a close and a reopen.
              onClick={(e) => { e.preventDefault(); void run(inUse != null); }}
            >
              {busy ? "Working…" : inUse ? "Archive it" : "Delete template"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
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
    <div className="rounded-xl surface-raised p-4">
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
