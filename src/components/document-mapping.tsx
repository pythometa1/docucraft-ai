/**
 * Document Mapping — the deterministic pipeline, in one place.
 *
 * The project wizard's numbered stages drive the older draft path, which fills
 * nothing: it has no manifest, so a template with `<placeholders>` comes back
 * out exactly as it went in. Everything that actually fills a document lives
 * behind a small "Open in Studio" button on a template row, which is why people
 * spend an afternoon on the wizard and conclude the product is broken.
 *
 * This tab is that pipeline, laid out as the four things a person actually does:
 *
 *   1  Compile   read the template, find its fields and conditions
 *   2  Review    see what was found, and approve it
 *   3  Map       bind each field to a column of the spreadsheet
 *   4  Generate  produce one document per row, and download them
 *
 * Each step shows what the engine decided and why. The bands come from §13, and
 * a mapping in REVIEW is not an error -- it is the system saying a person should
 * look before it writes somebody's salary into a letter.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import {
  AlertTriangle, ArrowRight, CheckCircle2, ChevronDown, ChevronUp, Download, FileText,
  Loader2, RefreshCw, ShieldCheck, Sparkles, Table2, Wand2,
} from "lucide-react";

import { api } from "@/lib/api";
import { CompileProgressList, useCompileProgress } from "@/components/compile-progress";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useStore } from "@/lib/store";
import { cn } from "@/lib/utils";

type Step = 1 | 2 | 3;

/** A source value that selects none of the branches the template offers. */
type UnmatchedValue = {
  field_id: string;
  column: string | null;
  observed_value: string;
  offered: string[];
};

type ManifestWarning = {
  code?: string;
  message?: string;
  detail?: string;
  evidence?: string;
  paragraph_index?: number;
};

type WarningDisposition = { resolved_by?: string; resolved_at?: string; note?: string };

const BAND_STYLE: Record<string, string> = {
  AUTO_ACCEPT: "bg-emerald-500/10 text-emerald-500 border-emerald-500/30",
  CONFIRM: "bg-sky-500/10 text-sky-500 border-sky-500/30",
  REVIEW: "bg-amber-500/10 text-amber-500 border-amber-500/30",
  BLOCK: "bg-rose-500/10 text-rose-500 border-rose-500/30",
};

const BAND_MEANING: Record<string, string> = {
  AUTO_ACCEPT: "Strong evidence. Applied automatically, still reversible.",
  CONFIRM: "Good evidence. Pre-selected — one click to accept.",
  REVIEW: "Worth a look before this writes into a letter.",
  BLOCK: "No usable evidence. Pick a column by hand.",
};

const TERMINAL_JOB_STATES = ["completed", "completed_with_errors", "failed", "blocked"];

// A batch runs for minutes and will cross the odd blip. One failed poll is not
// a dead job, so a couple are absorbed; past that polling stops *and says so*,
// rather than ending forever on the first 5xx with the status line frozen and
// every button re-enabled as though nothing had happened.
const POLL_TOLERANCE = 2;
const POLL_INTERVAL_MS = 1200;

const CONDITIONS_SHOWN = 5;
const FAILURES_SHOWN = 6;
const ROWS_SHOWN = 6;

function Pill({ band }: { band?: string }) {
  if (!band) return null;
  return (
    <span
      title={BAND_MEANING[band] ?? band}
      className={cn("shrink-0 rounded-md border px-1.5 py-0.5 text-[10px] font-medium", BAND_STYLE[band] ?? "")}
    >
      {band.replace("_", " ")}
    </span>
  );
}

function StepHeader({ step, active, done, title, hint }: {
  step: Step; active: boolean; done: boolean; title: string; hint: string;
}) {
  return (
    <div className="flex items-center gap-3">
      <div className={cn(
        "flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold",
        done ? "bg-emerald-500 text-white" : active ? "bg-gradient-brand text-white" : "bg-muted text-muted-foreground",
      )}>
        {done ? <CheckCircle2 className="h-4 w-4" /> : step}
      </div>
      <div>
        <div className="text-sm font-medium">{title}</div>
        <div className="text-xs text-muted-foreground">{hint}</div>
      </div>
    </div>
  );
}

function ShowMore({ total, limit, expanded, onToggle }: {
  total: number; limit: number; expanded: boolean; onToggle: () => void;
}) {
  if (total <= limit) return null;
  return (
    <button
      type="button" onClick={onToggle}
      className="inline-flex items-center gap-1 text-[11px] font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
    >
      {expanded
        ? <><ChevronUp className="h-3 w-3" /> Show fewer</>
        : <><ChevronDown className="h-3 w-3" /> Show all {total}</>}
    </button>
  );
}

/** Dispositions are keyed by warning code, so one judgement answers every
 *  occurrence of that code. Grouping here says so on screen instead of offering
 *  five buttons that all do the same thing. */
function groupWarnings(warnings: ManifestWarning[]) {
  const groups = new Map<string, { code: string; message: string; occurrences: ManifestWarning[] }>();
  for (const w of warnings) {
    const code = w?.code ?? "UNKNOWN";
    const group = groups.get(code) ?? { code, message: "", occurrences: [] };
    if (!group.message && w?.message) group.message = w.message;
    group.occurrences.push(w);
    groups.set(code, group);
  }
  return [...groups.values()];
}

function formatWhen(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function DocumentMapping({ project }: { project: any }) {
  const templates: any[] = project?.templates ?? [];
  const sources: any[] = project?.sources ?? [];
  const currentUser = useStore((s) => s.currentUser);

  const [templateId, setTemplateId] = useState<string>(templates[0]?.id ?? "");
  const [sourceId, setSourceId] = useState<string>(sources[0]?.id ?? "");

  const source = sources.find((s: any) => s.id === sourceId);
  const sourceVersionId: string = source?.currentVersionId ?? "";
  const sourceType: string = source?.type ?? "";

  const [sheets, setSheets] = useState<string[]>([]);
  const [sheet, setSheet] = useState<string>("");
  const [sheetsError, setSheetsError] = useState<string | null>(null);

  const [manifest, setManifest] = useState<any>(null);
  const [validation, setValidation] = useState<any>(null);
  const [suggestions, setSuggestions] = useState<any[]>([]);
  const [columns, setColumns] = useState<string[]>([]);
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [unmatched, setUnmatched] = useState<UnmatchedValue[]>([]);
  const [valueMap, setValueMap] = useState<Record<string, Record<string, string>>>({});
  const [job, setJob] = useState<any>(null);

  const [pollTick, setPollTick] = useState(0);
  const [pollStopped, setPollStopped] = useState(false);
  const [pollError, setPollError] = useState<string | null>(null);
  const pollFailures = useRef(0);

  const [ack, setAck] = useState<{ code: string; note: string } | null>(null);
  // Re-compiling throws away the approval, the column mappings, the branch value
  // map and the job handle. When a batch has already finished, `job.job_id` is
  // the only route back to its ZIP -- there is no job history on this screen --
  // so this is destructive in a way the button's label does not suggest.
  const [pendingRecompile, setPendingRecompile] = useState<null | { agentic: boolean }>(null);
  const [showAllConditions, setShowAllConditions] = useState(false);
  const [showAllFailures, setShowAllFailures] = useState(false);
  const [showAllRows, setShowAllRows] = useState(false);

  const [busy, setBusy] = useState<string | null>(null);
  const compiling = busy === "compile" || busy === "verify";
  const { newToken: newCompileToken, stages: compileStages, failed: compileFailed } = useCompileProgress(compiling);
  const step: Step = manifest?.status !== "approved" ? 1 : !job ? 2 : 3;

  // Suggestions, bindings, the value map and any running job are all derived
  // from one (template, source, sheet) triple. Left on screen after that triple
  // changes they are worse than stale: generate() posts `bindings` -- the
  // previous spreadsheet's column names -- against the new source_version_id,
  // and writes whatever happens to sit under those headers into every letter.
  const resetDerived = useCallback(() => {
    setSuggestions([]);
    setColumns([]);
    setBindings({});
    setUnmatched([]);
    setValueMap({});
    setJob(null);
    setPollStopped(false);
    setPollError(null);
    setShowAllRows(false);
    pollFailures.current = 0;
  }, []);

  // An existing manifest means this template has been here before; picking it up
  // rather than re-compiling avoids paying for a model call to learn what is
  // already known.
  useEffect(() => {
    if (!templateId) return;
    setManifest(null); setValidation(null); setShowAllConditions(false); setShowAllFailures(false);
    resetDerived();
    // Guarded like the sheets effect below. Switching template A -> B while A's
    // request is in flight let A's response land after B's reset, so the screen
    // showed A's manifest while `templateId` was B: Approve would approve A, and
    // step 3 would bind B's spreadsheet against A's fields.
    let live = true;
    api.listManifests(templateId)
      .then((r) => {
        if (!live) return;
        const latest = (r.items ?? [])[0];
        if (latest) setManifest(latest);
      })
      // A template with nothing compiled answers 200 with an empty list, so a
      // rejection here is a real failure, not a first visit.
      .catch((e: any) => {
        if (!live) return;
        toast.error("Could not look for an existing manifest", { description: e?.message ?? String(e) });
      });
    return () => { live = false; };
  }, [templateId, resetDerived]);

  // Which sheet a workbook is read from is a choice, and until now it was made
  // silently by the backend falling through to worksheets[0]. The names come
  // from the records endpoint, which returns them for xlsx only.
  useEffect(() => {
    resetDerived();
    setSheets([]); setSheet(""); setSheetsError(null);
    if (!sourceVersionId || sourceType !== "xlsx") return;
    let live = true;
    api.sourceRecords(sourceVersionId, 1)
      .then((r) => {
        if (!live) return;
        const names = r.sheets ?? [];
        setSheets(names);
        setSheet(names[0] ?? "");
      })
      .catch((e: any) => {
        if (!live) return;
        // Not fatal -- mapping still runs against the default sheet -- but a
        // workbook whose tabs could not be listed must say so rather than look
        // like a single-sheet file.
        setSheetsError(e?.message ?? String(e));
      });
    return () => { live = false; };
  }, [sourceId, sourceVersionId, sourceType, resetDerived]);

  const refreshValidation = useCallback(async (manifestId: string) => {
    try {
      setValidation(await api.manifestValidation(manifestId));
    } catch (e: any) {
      setValidation(null);
      toast.error("Could not read the approval checks", { description: e?.message ?? String(e) });
    }
  }, []);

  useEffect(() => {
    if (!manifest?.id) return;
    void refreshValidation(manifest.id);
  }, [manifest?.id, manifest?.status, refreshValidation]);

  async function compile(agentic = false) {
    const progressToken = newCompileToken();
    setBusy(agentic ? "verify" : "compile");
    try {
      const m = await api.compileManifest(templateId, { agentic, progressToken });
      setManifest(m);
      setShowAllConditions(false); setShowAllFailures(false);
      resetDerived();
      const conditions = (m.conditions ?? []).length;
      toast.success(
        `Compiled ${(m.fields ?? []).length} fields and ${conditions} condition${conditions === 1 ? "" : "s"}` +
        (m.compiled_by?.startsWith("llm") ? " (read by a model)" : " (no model needed)"),
      );
    } catch (e: any) {
      toast.error("Could not compile this template", { description: e?.message ?? String(e) });
    } finally { setBusy(null); }
  }

  async function approve() {
    if (!manifest?.id) return;
    const manifestId = manifest.id;
    setBusy("approve");
    try {
      await api.approveManifest(manifestId);
      setManifest((m: any) => (m ? { ...m, status: "approved" } : m));
      toast.success("Manifest approved — this is what production will run.");
    } catch (e: any) {
      toast.error("Could not approve", { description: e?.message ?? String(e) });
      // A refusal here means the blockers moved under us (four eyes, a blocked
      // mapping, a warning someone else answered). Show the current set rather
      // than the one this screen was drawn from.
      await refreshValidation(manifestId);
    } finally { setBusy(null); }
  }

  async function acknowledgeWarning() {
    if (!ack || !manifest?.id) return;
    const manifestId = manifest.id;
    const { code, note } = ack;
    setBusy(`warning:${code}`);
    try {
      const r = await api.resolveManifestWarning(manifestId, code, note.trim());
      setManifest((m: any) => (m ? { ...m, warning_dispositions: r?.dispositions ?? m.warning_dispositions } : m));
      setAck(null);
      toast.success(`Acknowledged ${code}`, { description: "Recorded on this manifest under your name." });
      await refreshValidation(manifestId);
    } catch (e: any) {
      toast.error("Could not record that acknowledgement", { description: e?.message ?? String(e) });
    } finally { setBusy(null); }
  }

  async function loadSuggestions() {
    if (!manifest?.id) return;
    if (!sources.length) {
      toast.error("No spreadsheet yet", {
        description: "Download the data template above, fill it in, and upload it as a source.",
      });
      return;
    }
    if (!sourceVersionId) { toast.error("That upload has not finished processing yet."); return; }
    setBusy("suggest");
    try {
      const r = await api.bindingSuggestions(manifest.id, sourceVersionId, sheet || undefined);
      setSuggestions(r.suggestions ?? []);
      setColumns(r.columns ?? []);
      const seed: Record<string, string> = {};
      for (const s of r.suggestions ?? []) if (s.column) seed[s.field_id] = s.column;
      setBindings(seed);
      setUnmatched(r.unmatched_condition_values ?? []);
      setValueMap({});
    } catch (e: any) {
      toast.error("Could not read the spreadsheet", { description: e?.message ?? String(e) });
    } finally { setBusy(null); }
  }

  async function generate() {
    if (!manifest?.id) return;
    if (!sources.length) {
      toast.error("No spreadsheet yet", {
        description: "Download the data template above, fill it in, and upload it as a source.",
      });
      return;
    }
    if (!sourceVersionId) { toast.error("That upload has not finished processing yet."); return; }
    setBusy("generate");
    try {
      const field_bindings = Object.fromEntries(Object.entries(bindings).filter(([, column]) => !!column));
      await api.saveBinding(manifest.id, {
        source_version_id: sourceVersionId,
        field_bindings,
        // Vocabulary the reviewer reconciled in step 3. Without it every row
        // carrying an unmatched value generates with its conditional section
        // missing, and nothing says so until a QA note after the batch.
        ...(Object.keys(valueMap).length ? { value_map: valueMap } : {}),
      });
      pollFailures.current = 0;
      setPollStopped(false); setPollError(null); setShowAllRows(false);
      const started = await api.generateBatch(manifest.id, { source_version_id: sourceVersionId, language: "en" });
      setJob({ job_id: started.job_id, status: started.status });
      toast.success("Generating…");
    } catch (e: any) {
      toast.error("Could not start generation", { description: e?.message ?? String(e) });
      setBusy(null);
    }
  }

  // Poll while the batch runs. The canary set renders first and has to pass
  // before the rest of the rows are attempted, so an early failure shows up here
  // rather than after a thousand documents.
  useEffect(() => {
    if (!job?.job_id) return;
    if (TERMINAL_JOB_STATES.includes(job.status)) { setBusy(null); return; }
    if (pollStopped) return;
    const timer = setTimeout(() => {
      // Keep the id we started with. `POST .../generate-batch` answers with
      // `job_id` and `GET /jobs/{id}` answers with `id`, so replacing the whole
      // object on each poll dropped the identifier -- and the download button,
      // which only renders once polling has finished, then asked for
      // `/jobs/undefined/download` and reported the archive as unavailable.
      api.getJob(job.job_id)
        .then((next: any) => {
          pollFailures.current = 0;
          setPollError(null);
          setJob({ ...next, job_id: job.job_id });
        })
        .catch((e: any) => {
          pollFailures.current += 1;
          const reason = e?.message ?? String(e);
          if (pollFailures.current > POLL_TOLERANCE) {
            setPollStopped(true);
            setPollError(reason);
            setBusy(null);
            toast.error("Lost track of this batch", {
              description: `${reason} The documents may still be finishing on the server — press "Check again".`,
            });
            return;
          }
          // Nothing in `job` changed, so the effect will not re-run on its own:
          // bump the tick to re-arm the timer instead of quietly stopping.
          setPollError(`${reason} — retrying (${pollFailures.current} of ${POLL_TOLERANCE}).`);
          setPollTick((t) => t + 1);
        });
    }, POLL_INTERVAL_MS);
    return () => clearTimeout(timer);
  }, [job, pollStopped, pollTick]);

  function resumePolling() {
    pollFailures.current = 0;
    setPollStopped(false);
    setPollError(null);
    setBusy("generate");
    setPollTick((t) => t + 1);
  }

  async function downloadSourceTemplate() {
    if (!manifest?.id) return;
    setBusy("srctemplate");
    try {
      const { url, filename } = await api.sourceTemplate(manifest.id);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      // Appended before clicking and revoked on the next tick: a detached
      // anchor's click() does nothing in Firefox, and revoking synchronously
      // can cancel the download before the browser has read the blob.
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      toast.success("Data template downloaded", {
        description: "Fill it in and upload it as a source — the columns already match this template.",
      });
    } catch (e: any) {
      toast.error("Could not build the data template", { description: e?.message ?? String(e) });
    } finally { setBusy(null); }
  }

  async function download() {
    if (!job?.job_id) return;
    setBusy("download");
    try {
      const url = await api.batchZipUrl(job.job_id);
      const a = document.createElement("a");
      a.href = url; a.download = `${project.name}-documents.zip`; a.click();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      toast.error("Could not download", { description: e?.message ?? String(e) });
    } finally { setBusy(null); }
  }

  // Only a template is required to get in. This used to demand a spreadsheet as
  // well, which made "Download data template" unreachable by anyone who did not
  // already have one -- and not having one is the entire reason to want it.
  // Compiling needs the template alone; the source is what steps 3 and 4 need.
  if (!templates.length) {
    return (
      <div className="rounded-xl border border-dashed border-border p-8 text-center">
        <Table2 className="mx-auto h-8 w-8 text-muted-foreground" />
        <p className="mt-3 text-sm font-medium">Upload a template first</p>
        <p className="mt-1 text-xs text-muted-foreground">
          Document Mapping reads the template to work out what data it needs. Once it has, it can
          hand you a spreadsheet with the right columns already in it.
        </p>
      </div>
    );
  }

  const progress = job?.progress ?? {};
  const rows: any[] = progress.rows ?? [];
  const shownRows = showAllRows ? rows : rows.slice(0, ROWS_SHOWN);

  const conditions: any[] = manifest?.conditions ?? [];
  const shownConditions = showAllConditions ? conditions : conditions.slice(0, CONDITIONS_SHOWN);

  const warnings: ManifestWarning[] = validation?.warnings ?? manifest?.warnings ?? [];
  const dispositions: Record<string, WarningDisposition> =
    validation?.warning_dispositions ?? manifest?.warning_dispositions ?? {};
  const warningGroups = groupWarnings(warnings);
  const acknowledgedCount = warningGroups.filter((g) => dispositions[g.code]).length;
  const ackWarning = ack ? warningGroups.find((g) => g.code === ack.code) : undefined;
  const ackInFlight = !!ack && busy === `warning:${ack.code}`;

  const allFailures: any[] = validation?.failures ?? [];
  // Every undispositioned warning also arrives as a validation failure. Showing
  // it in both places reads as two problems, so when the warnings panel is
  // carrying them the failure list drops them and points upwards instead.
  const warningFailures = allFailures.filter((f) => f.rule === "unresolved_compiler_warning");
  const failures = warningGroups.length
    ? allFailures.filter((f) => f.rule !== "unresolved_compiler_warning")
    : allFailures;
  const shownFailures = showAllFailures ? failures : failures.slice(0, FAILURES_SHOWN);

  const mappedCount = Object.values(bindings).filter(Boolean).length;
  const unmappedValueCount = unmatched.filter((u) => !valueMap[u.field_id]?.[u.observed_value]).length;
  // The client cannot pass `sheet` to generate-batch, so a batch always reads
  // the workbook's first sheet. Mapping against another one and then generating
  // would fill this sheet's headers from that sheet's rows.
  const sheetBlocksGeneration = sheets.length > 1 && !!sheet && sheet !== sheets[0];

  return (
    <div className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1.5">
          <span className="text-xs font-medium text-muted-foreground">Template</span>
          <select
            value={templateId} onChange={(e) => setTemplateId(e.target.value)} disabled={!!busy}
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm disabled:opacity-50"
          >
            {templates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </label>
        <label className="space-y-1.5">
          <span className="text-xs font-medium text-muted-foreground">Source spreadsheet</span>
          <select
            value={sourceId} onChange={(e) => setSourceId(e.target.value)} disabled={!!busy || !sources.length}
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm disabled:opacity-50"
          >
            {sources.length
              ? sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)
              : <option value="">None yet — compile first, then download the data template</option>}
          </select>
        </label>
      </div>

      {sheets.length > 1 && (
        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-muted-foreground">
            Sheet <span className="font-normal">({sheets.length} in this workbook)</span>
          </span>
          <select
            value={sheet}
            onChange={(e) => { setSheet(e.target.value); resetDerived(); }}
            disabled={!!busy}
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm disabled:opacity-50 sm:w-1/2"
          >
            {sheets.map((s, i) => <option key={s} value={s}>{i === 0 ? `${s} (default)` : s}</option>)}
          </select>
        </label>
      )}

      {sheetsError && (
        <div className="flex items-start gap-1.5 text-xs text-amber-500">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>Could not list the sheets in this workbook, so the first one will be used: {sheetsError}</span>
        </div>
      )}

      {/* Compiling now happens on the Template stage, where it belongs: it is a
          fact about the template, not about this mapping. What stays here is the
          way back -- a template that arrived uncompiled, or one whose wording
          changed and has to be read again. */}
      <section className="rounded-xl border border-border bg-background/40 p-4 space-y-3">
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={() => (manifest ? setPendingRecompile({ agentic: false }) : compile(false))} disabled={!!busy}>
            {busy === "compile" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}
            {manifest ? "Re-compile" : "Compile"}
          </Button>
          <Button size="sm" variant="outline"
            onClick={() => (manifest ? setPendingRecompile({ agentic: true }) : compile(true))} disabled={!!busy}
            title="Compile, fill test rows, read the QA failures, and revise. Slower, and only useful where the rules struggled.">
            {busy === "verify" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
            Compile &amp; self-verify
          </Button>
        </div>
        {/* Only while it runs: once there is a manifest, the summary line below
            says what came out of it, and repeating the stages would be noise. */}
        {compiling && <CompileProgressList stages={compileStages} failed={compileFailed} />}
        {manifest && (
          <div className="space-y-2">
            <div className="text-xs text-muted-foreground">
              <span className="font-medium text-foreground">{(manifest.fields ?? []).length}</span> fields ·{" "}
              <span className="font-medium text-foreground">{conditions.length}</span> conditions ·{" "}
              read by <span className="font-mono">{manifest.compiled_by}</span> · confidence {Math.round((manifest.confidence ?? 0) * 100)}%
            </div>
            {/* Also offered on the Template stage, which is where someone who has
                no spreadsheet yet will be. Kept here too because a re-compile can
                change the columns, and the sheet in their hands is then stale. */}
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" onClick={downloadSourceTemplate} disabled={!!busy}>
                {busy === "srctemplate" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                Download data template
              </Button>
              <span className="text-xs text-muted-foreground">
                A spreadsheet with this template&rsquo;s columns already named, and a fixed list of
                choices on every column that decides which sections are kept.
              </span>
            </div>
          </div>
        )}
      </section>

      {/* 2 — review and approve */}
      {manifest && (
        <section className="rounded-xl border border-border bg-background/40 p-4 space-y-3">
          <StepHeader step={1} active={step === 1} done={manifest.status === "approved"}
            title="Review and approve" hint="Nothing generates from a manifest nobody approved" />

          {/* §7: an approver signs the meaning, not the syntax. The sentence the
              server renders from the stored expression leads; the expression
              itself stays visible underneath so an engineer can still audit it,
              but it is no longer the only thing on offer. A condition that
              cannot be rendered says so — it is the one that must not be
              approved, so it must not be the one that looks like a blank. */}
          {conditions.length > 0 && (
            <div className="pl-10 space-y-2">
              <ul className="space-y-2">
                {shownConditions.map((c: any) => (
                  <li key={c.id} className="space-y-0.5">
                    {c.plain_english ? (
                      <span className="text-xs text-foreground" title={c.approval_sentence ?? undefined}>
                        Keep when {c.plain_english}
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1.5 text-xs text-amber-500">
                        <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                        This rule could not be put into words{c.plain_english_error ? `: ${c.plain_english_error}` : ""}
                      </span>
                    )}
                    <div className="font-mono text-[11px] text-muted-foreground">{c.expression}</div>
                  </li>
                ))}
              </ul>
              <ShowMore total={conditions.length} limit={CONDITIONS_SHOWN} expanded={showAllConditions}
                onToggle={() => setShowAllConditions((v) => !v)} />
            </div>
          )}

          {/* The compiler's warnings are not advisory: the validator turns every
              undispositioned one into a blocker, so this list is the only way
              past a manifest the compiler had doubts about. */}
          {warningGroups.length > 0 && (
            <div className="ml-10 space-y-2 rounded-lg border border-border p-3">
              <div className="flex items-center gap-1.5 text-xs font-medium">
                <ShieldCheck className="h-3.5 w-3.5 text-muted-foreground" />
                Compiler warnings — {acknowledgedCount} of {warningGroups.length} acknowledged
              </div>
              <p className="text-[11px] text-muted-foreground">
                Approval stays blocked until each one has a judgement against it. Acknowledging is not a
                dismissal: your name, the time and your note are stored on the manifest and shown to
                everyone who reads it afterwards.
              </p>
              {warningGroups.map((g) => {
                const disposition = dispositions[g.code];
                return (
                  <div key={g.code} className="space-y-1 rounded-md border border-border/60 p-2">
                    <div className="flex flex-wrap items-start gap-2">
                      <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">{g.code}</span>
                      <span className="min-w-[12rem] flex-1 text-xs">
                        {g.message || g.occurrences[0]?.detail || "This paragraph needs a human decision."}
                        {g.occurrences.length > 1 && (
                          <span className="text-muted-foreground"> · {g.occurrences.length} places</span>
                        )}
                      </span>
                      {disposition ? (
                        <span className="inline-flex shrink-0 items-center gap-1.5 text-[11px] text-emerald-500">
                          <CheckCircle2 className="h-3.5 w-3.5" /> Acknowledged
                        </span>
                      ) : manifest.status === "approved" ? null : (
                        <Button size="sm" variant="outline" className="shrink-0"
                          onClick={() => setAck({ code: g.code, note: "" })} disabled={!!busy}>
                          {busy === `warning:${g.code}`
                            ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            : <ShieldCheck className="h-3.5 w-3.5" />}
                          Acknowledge…
                        </Button>
                      )}
                    </div>
                    <ul className="space-y-0.5">
                      {g.occurrences.map((w, i) => (
                        <li key={i} className="text-[11px] text-muted-foreground">
                          {typeof w.paragraph_index === "number" && w.paragraph_index >= 0
                            ? `Paragraph ${w.paragraph_index}: `
                            : ""}
                          {w.detail ?? ""}
                          {w.evidence ? ` — ${w.evidence}` : ""}
                        </li>
                      ))}
                    </ul>
                    {disposition && (
                      <div className="text-[11px] text-muted-foreground">
                        Acknowledged {formatWhen(disposition.resolved_at)}
                        {disposition.note ? ` — “${disposition.note}”` : " — no note given"}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {validation && !validation.can_approve && (shownFailures.length > 0 || warningFailures.length > 0) && (
            <div className="ml-10 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 space-y-1.5">
              <div className="flex items-center gap-1.5 text-xs font-medium text-amber-500">
                <AlertTriangle className="h-3.5 w-3.5" /> Resolve before approving
              </div>
              {shownFailures.map((f: any, i: number) => (
                <div key={i} className="text-xs text-muted-foreground">{f.detail}</div>
              ))}
              <ShowMore total={failures.length} limit={FAILURES_SHOWN} expanded={showAllFailures}
                onToggle={() => setShowAllFailures((v) => !v)} />
              {warningGroups.length > 0 && warningFailures.length > 0 && (
                <div className="text-xs text-muted-foreground">
                  {warningFailures.length} compiler warning{warningFailures.length === 1 ? "" : "s"} still
                  need a judgement — see the list above.
                </div>
              )}
            </div>
          )}

          <div className="pl-10">
            {manifest.status === "approved" ? (
              <span className="inline-flex items-center gap-1.5 text-xs text-emerald-500">
                <CheckCircle2 className="h-3.5 w-3.5" /> Approved
              </span>
            ) : (
              <Button size="sm" onClick={approve} disabled={!!busy || (validation ? !validation.can_approve : false)}>
                {busy === "approve" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                Approve
              </Button>
            )}
          </div>
        </section>
      )}

      {/* 3 — map fields to columns */}
      {manifest?.status === "approved" && (
        <section className="rounded-xl border border-border bg-background/40 p-4 space-y-3">
          <StepHeader step={2} active={step === 2} done={mappedCount > 0}
            title="Map fields to columns" hint="This is the step that connects the spreadsheet to the letter" />

          {!sources.length && (
            <div className="ml-10 rounded-lg border border-dashed border-border p-3 text-xs text-muted-foreground">
              No spreadsheet on this project yet. Use <span className="font-medium text-foreground">Download data
              template</span> in step 1 — it already has this template&rsquo;s columns and a fixed list of choices
              on the ones that decide which sections are kept — then upload it under Sources.
            </div>
          )}
          <div className="pl-10">
            <Button size="sm" variant="outline" onClick={loadSuggestions} disabled={!!busy || !sources.length}>
              {busy === "suggest" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              {suggestions.length ? "Re-read spreadsheet" : "Match columns"}
            </Button>
            {sheets.length > 1 && (
              <span className="ml-2 text-[11px] text-muted-foreground">reading “{sheet}”</span>
            )}
          </div>

          {/* Rows whose condition value selects no branch. Each is a letter that
              would generate with a section missing, and here it still costs one
              line to fix. */}
          {unmatched.length > 0 && (
            <div className="ml-10 space-y-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3">
              <div className="flex items-center gap-1.5 text-xs font-medium text-amber-500">
                <AlertTriangle className="h-3.5 w-3.5" />
                {unmatched.length} spreadsheet value{unmatched.length === 1 ? "" : "s"} match no branch of this template
              </div>
              <p className="text-[11px] text-muted-foreground">
                Every row carrying one of these generates with its conditional section silently missing.
                Point each value at the branch it means — the answer applies to every row that carries it.
              </p>
              {unmatched.map((u) => (
                <div key={`${u.field_id}|${u.observed_value}`} className="flex flex-wrap items-center gap-2">
                  <span className="text-[11px]" title={`${u.field_id}${u.column ? ` · column ${u.column}` : ""}`}>
                    <span className="font-mono">{u.column ?? u.field_id}</span> = “{u.observed_value}”
                  </span>
                  <ArrowRight className="h-3 w-3 shrink-0 text-muted-foreground" />
                  <select
                    value={valueMap[u.field_id]?.[u.observed_value] ?? ""}
                    onChange={(e) => {
                      const chosen = e.target.value;
                      setValueMap((prev) => {
                        const forField = { ...(prev[u.field_id] ?? {}) };
                        if (chosen) forField[u.observed_value] = chosen;
                        else delete forField[u.observed_value];
                        const next = { ...prev, [u.field_id]: forField };
                        if (!Object.keys(forField).length) delete next[u.field_id];
                        return next;
                      });
                    }}
                    className="min-w-[10rem] rounded-md border border-border bg-background px-2 py-1 text-xs"
                  >
                    <option value="">— leave unmapped —</option>
                    {u.offered.map((o) => <option key={o} value={o}>{o}</option>)}
                  </select>
                </div>
              ))}
              {unmappedValueCount > 0 && (
                <div className="text-[11px] font-medium text-amber-500">
                  {unmappedValueCount} still unmapped — those rows will generate with the section missing.
                </div>
              )}
            </div>
          )}

          {suggestions.length > 0 && (
            <div className="ml-10 divide-y divide-border rounded-lg border border-border">
              {suggestions.map((s: any) => (
                <div key={s.field_id} className="flex items-center gap-2 p-2">
                  <span className="w-52 shrink-0 truncate font-mono text-[11px]" title={s.field_id}>{s.field_id}</span>
                  <ArrowRight className="h-3 w-3 shrink-0 text-muted-foreground" />
                  <select
                    value={bindings[s.field_id] ?? ""}
                    onChange={(e) => setBindings({ ...bindings, [s.field_id]: e.target.value })}
                    className="flex-1 rounded-md border border-border bg-background px-2 py-1 text-xs"
                  >
                    <option value="">— not mapped —</option>
                    {columns.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                  <Pill band={s.band} />
                </div>
              ))}
            </div>
          )}

          {suggestions.length > 0 && (
            <div className="pl-10 text-xs text-muted-foreground">
              {mappedCount} of {suggestions.length} mapped.
              {suggestions.some((s) => !s.column) && " Fields with no match need a column choosing by hand."}
            </div>
          )}
        </section>
      )}

      {/* 4 — generate */}
      {manifest?.status === "approved" && suggestions.length > 0 && (
        <section className="rounded-xl border border-border bg-background/40 p-4 space-y-3">
          <StepHeader step={3} active={step === 3} done={job?.status === "completed"}
            title="Generate documents" hint="One document per row, checked before it is kept" />

          <div className="pl-10 space-y-2">
            <Button size="sm" onClick={generate} disabled={!!busy || !mappedCount || sheetBlocksGeneration}>
              {busy === "generate" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileText className="h-3.5 w-3.5" />}
              Generate
            </Button>
            {sheetBlocksGeneration && (
              <div className="flex items-start gap-1.5 text-xs text-amber-500">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>
                  A batch always reads “{sheets[0]}”, the workbook's first sheet — this client cannot yet
                  send the chosen sheet to the generator. Mapping against “{sheet}” is safe to review;
                  switch back to “{sheets[0]}” to generate.
                </span>
              </div>
            )}
          </div>

          {job && (
            <div className="ml-10 space-y-2">
              <div className="text-xs text-muted-foreground">
                {job.status === "blocked"
                  ? <span className="text-rose-500">{job.error}</span>
                  : <>Status <span className="font-medium text-foreground">{job.status}</span>
                      {progress.rows_total != null && <> · {progress.rows_done ?? 0} of {progress.rows_total}</>}
                      {progress.blocked ? <> · <span className="text-rose-500">{progress.blocked} blocked</span></> : null}</>}
              </div>

              {pollError && (
                <div className="flex flex-wrap items-center gap-2 text-xs text-amber-500">
                  <span className="inline-flex items-start gap-1.5">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    {pollStopped
                      ? `Stopped watching this batch: ${pollError} It may still have finished.`
                      : pollError}
                  </span>
                  {pollStopped && (
                    <Button size="sm" variant="outline" onClick={resumePolling} disabled={!!busy}>
                      <RefreshCw className="h-3.5 w-3.5" /> Check again
                    </Button>
                  )}
                </div>
              )}

              {shownRows.map((r: any) => (
                <div key={r.row_index} className="flex items-center gap-2 text-[11px]">
                  <span className={cn("rounded px-1.5 py-0.5",
                    r.status === "generated" ? "bg-emerald-500/10 text-emerald-500"
                      : r.status === "blocked" ? "bg-rose-500/10 text-rose-500"
                      : "bg-amber-500/10 text-amber-500")}>
                    row {r.row_index} · {r.status}
                  </span>
                  {r.is_canary && <span className="text-muted-foreground">canary</span>}
                  {(r.qa_notes ?? []).length > 0 && (
                    <span className="truncate text-muted-foreground" title={r.qa_notes.join("; ")}>
                      {r.qa_notes[0]}
                    </span>
                  )}
                </div>
              ))}
              <ShowMore total={rows.length} limit={ROWS_SHOWN} expanded={showAllRows}
                onToggle={() => setShowAllRows((v) => !v)} />

              {["completed", "completed_with_errors"].includes(job.status) && (
                <Button size="sm" variant="outline" onClick={download} disabled={!!busy}>
                  {busy === "download" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                  Download all
                </Button>
              )}
            </div>
          )}
        </section>
      )}

      <AlertDialog open={!!pendingRecompile} onOpenChange={(o) => !o && setPendingRecompile(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Re-compile this template?</AlertDialogTitle>
            <AlertDialogDescription>
              <span className="block">
                A new manifest replaces the current one. Its approval does not carry over, and neither do the
                warning acknowledgements recorded against it — both have to be done again.
              </span>
              <span className="mt-2 block">
                The column mappings and branch values on this screen are cleared.
                {job?.status === "completed" || job?.status === "completed_with_errors"
                  ? " The finished batch's download link is lost with them: documents already generated stay in the project, but the archive of this run cannot be rebuilt from here."
                  : ""}
              </span>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault();
                const agentic = pendingRecompile?.agentic ?? false;
                setPendingRecompile(null);
                void compile(agentic);
              }}
            >
              Re-compile
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={!!ack}
        onOpenChange={(open) => { if (!open && !ackInFlight) setAck(null); }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Acknowledge {ack?.code}</AlertDialogTitle>
            <AlertDialogDescription>
              <span className="block">
                {ackWarning?.message || ackWarning?.occurrences[0]?.detail || "This warning needs a human decision."}
              </span>
              <span className="mt-2 block">
                This records a judgement, not a dismissal. {currentUser ? `${currentUser} and` : "Your account and"} the
                time are stored on this manifest with your note, and stay visible to everyone who reviews it
                afterwards. The warning is not deleted — it stops blocking approval.
              </span>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <Textarea
            value={ack?.note ?? ""}
            onChange={(e) => setAck((a) => (a ? { ...a, note: e.target.value } : a))}
            disabled={ackInFlight}
            placeholder="Why is this safe to generate from? (optional, kept with your name)"
            className="min-h-[80px] text-sm"
          />
          <AlertDialogFooter>
            <AlertDialogCancel disabled={ackInFlight}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => { e.preventDefault(); void acknowledgeWarning(); }}
              disabled={ackInFlight}
            >
              {ackInFlight ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
              Record acknowledgement
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
