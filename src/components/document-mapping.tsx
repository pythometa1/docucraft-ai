/**
 * Document Mapping — pointing a template's fields at a spreadsheet's columns.
 *
 * Two steps, where there were four:
 *
 *   1  Map       bind each field to a column of the spreadsheet
 *   2  Generate  produce one document per row
 *
 * The other two are gone rather than moved. **Compile** was a button offering to
 * pay for a model call to re-learn what was already known: a template is read
 * when it is uploaded, and reading it again belongs on the template's own row
 * where the consequence is visible. **Review and approve** was a gate that could
 * not be passed without going through a review nobody had asked for --
 * `validate_manifest` turns every unacknowledged compiler warning into a
 * failure, and a freshly read template has warnings and no acknowledgements by
 * construction, so the real sequence was "acknowledge each warning in writing,
 * sign, then generate" for every template. Generating no longer asks whether
 * somebody signed the reading; it asks whether the reading is usable and
 * current.
 *
 * What survives from that step is the part worth keeping: the placeholders the
 * compiler could not claim are shown beside the Generate button, because each
 * one is a document that will come back blocked with "Leftover placeholder
 * brackets". They are advice, not a gate — the person about to press Generate is
 * the one who can judge them.
 *
 * The bands come from §13, and a mapping in REVIEW is not an error -- it is the
 * system saying a person should look before it writes somebody's salary into a
 * letter.
 */

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Download,
  FileText,
  Loader2,
  RefreshCw,
  Table2,
  Wand2,
} from "lucide-react";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { type BatchWatch } from "@/components/batch-progress";
import { cn } from "@/lib/utils";

type Step = 1 | 2;

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



// A batch runs for minutes and will cross the odd blip. One failed poll is not
// a dead job, so a couple are absorbed; past that polling stops *and says so*,
// rather than ending forever on the first 5xx with the status line frozen and
// every button re-enabled as though nothing had happened.


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

export function DocumentMapping({ project, watch, onGenerating }: {
  project: any;
  /** The batch this project is running, held by the project screen so it
   *  survives leaving this stage. */
  watch: BatchWatch;
  /** Called when a batch starts, so the screen can move to Documents -- which is
   *  where its output is going to appear. */
  onGenerating: () => void;
}) {
  const templates: any[] = project?.templates ?? [];
  const sources: any[] = project?.sources ?? [];

  const [templateId, setTemplateId] = useState<string>(templates[0]?.id ?? "");
  const [sourceId, setSourceId] = useState<string>(sources[0]?.id ?? "");

  const source = sources.find((s: any) => s.id === sourceId);
  const sourceVersionId: string = source?.currentVersionId ?? "";
  const sourceType: string = source?.type ?? "";

  const [sheets, setSheets] = useState<string[]>([]);
  const [sheet, setSheet] = useState<string>("");
  const [sheetsError, setSheetsError] = useState<string | null>(null);

  const [manifest, setManifest] = useState<any>(null);
  const [suggestions, setSuggestions] = useState<any[]>([]);
  const [columns, setColumns] = useState<string[]>([]);
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [unmatched, setUnmatched] = useState<UnmatchedValue[]>([]);
  const [valueMap, setValueMap] = useState<Record<string, Record<string, string>>>({});


  const [busy, setBusy] = useState<string | null>(null);
  // Two steps, where there were three. The first was "review and approve the
  // manifest", which reading a template now does for itself wherever that is
  // allowed -- so what is left is the work this screen is actually for: point the
  // template's fields at the spreadsheet's columns, then run the batch.
  const step: Step = !watch.job ? 1 : 2;

  // Suggestions, bindings and the value map are all derived from one
  // (template, source, sheet) triple. Left on screen after that triple
  // changes they are worse than stale: generate() posts `bindings` -- the
  // previous spreadsheet's column names -- against the new source_version_id,
  // and writes whatever happens to sit under those headers into every letter.
  const resetDerived = useCallback(() => {
    setSuggestions([]);
    setColumns([]);
    setBindings({});
    setUnmatched([]);
    setValueMap({});
    // The running batch is deliberately *not* cleared here. It belongs to the
    // project, not to this (template, source, sheet) triple, and dropping it on
    // a dropdown change was how a finished batch's archive became unreachable.
  }, []);

  // An existing manifest means this template has been here before; picking it up
  // rather than re-compiling avoids paying for a model call to learn what is
  // already known.
  useEffect(() => {
    if (!templateId) return;
    setManifest(null);
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

  // There was an approval block here: `refreshValidation`, `approve` and
  // `acknowledgeWarning`, plus the dialog that made you type a note for each
  // compiler warning before the manifest could be signed. All of it existed to
  // satisfy one rule -- generation refused a manifest nobody had approved -- and
  // that rule is gone for ordinary templates. `GET .../validation`,
  // `:approve` and `warnings:resolve` are all still served, and a template
  // flagged legally binding still needs them; this screen simply is not where
  // that happens.

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
      const started = await api.generateBatch(manifest.id, { source_version_id: sourceVersionId, language: "en" });
      watch.start({ job_id: started.job_id, status: started.status });
      toast.success("Generating…", {
        description: "Watch it on the Documents stage — that is where the letters appear.",
      });
      // Moved for the user, not away from them. The output of this is documents,
      // and the batch panel now lives beside them; leaving the reader here would
      // leave them watching a progress bar on a screen about column mappings.
      onGenerating();
    } catch (e: any) {
      toast.error("Could not start generation", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
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

  const conditions: any[] = manifest?.conditions ?? [];

  // Read straight off the manifest. This used to prefer `GET .../validation`,
  // which returned the same warnings alongside the approval blockers derived
  // from them -- and there are no approval blockers on this screen any more, so
  // the extra request bought a second copy of what the manifest already carries.
  //
  // The dispositions are gone with it. An acknowledgement was how a warning
  // stopped blocking approval; nothing blocks now, so a warning is either worth
  // reading or it is not, and there is nothing to record against it here.
  const warningGroups = groupWarnings((manifest?.warnings ?? []) as ManifestWarning[]);

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

      {/* Reading a template is a fact about the template, so it happens once,
          at upload, on the Template stage. There is no Compile button here and
          no Re-compile either: the second was destructive in a way its label did
          not suggest -- it threw away the approval, the column mappings, the
          branch value map and the job handle, and `job.job_id` was the only
          route back to a finished batch's ZIP. Reading a template again is done
          from the template's own row, where the consequence is visible.

          "Compile & self-verify" is gone for a different reason: it had already
          stopped being a second thing. The endpoint's signature is
          `(template_file_id, progress_token, db, user)` and FastAPI drops the
          `agentic` flag the button sent, so both buttons made the identical
          request and only the spinner's label differed. */}
      {!manifest ? (
        <section className="rounded-xl border border-dashed border-border p-6 text-center">
          <Wand2 className="mx-auto h-7 w-7 text-muted-foreground" />
          <p className="mt-2.5 text-sm font-medium">This template has not been read yet</p>
          <p className="mx-auto mt-1 max-w-md text-xs text-muted-foreground">
            Reading it is what works out which columns your spreadsheet needs. Go back to the
            Template stage and read it — the row there says whether it failed and why.
          </p>
        </section>
      ) : (
        <section className="rounded-xl border border-border bg-background/40 p-4 space-y-3">
          <div className="text-xs text-muted-foreground">
            <span className="font-medium text-foreground">{(manifest.fields ?? []).length}</span> fields ·{" "}
            <span className="font-medium text-foreground">{conditions.length}</span> conditions ·{" "}
            read by <span className="font-mono">{manifest.compiled_by}</span> · confidence {Math.round((manifest.confidence ?? 0) * 100)}%
          </div>
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
        </section>
      )}

      {/* What the compiler was unsure about, as advice rather than a gate.
          These used to be a wall: each one had to be acknowledged with a written
          note before the manifest could be approved, and generation refused an
          unapproved manifest -- so a template with three warnings was three
          dialogs and a signature away from producing anything.

          They are still worth reading, because they predict a specific failure:
          an unclaimed placeholder means every document generated from this
          template fails its QA check with "Leftover placeholder brackets", and
          finding that out here beats finding it out after a batch. But they
          describe a risk, and the person who can judge it is the one about to
          press Generate -- so they are shown next to that button, not in front
          of it. */}
      {manifest && warningGroups.length > 0 && (
        <section className="rounded-xl border border-warning/25 bg-warning/5 p-4">
          <p className="flex items-center gap-1.5 text-sm font-medium text-warning">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            {warningGroups.length} thing{warningGroups.length === 1 ? "" : "s"} the compiler was unsure about
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            None of these stop you generating. They are the places a document is most likely to come
            back with a QA failure, so they are worth a look first.
          </p>
          <ul className="mt-2 space-y-1">
            {warningGroups.slice(0, 4).map((g) => (
              <li key={g.code} className="text-xs text-muted-foreground">
                <span className="font-mono text-[10px] uppercase opacity-70">{g.code}</span>{" "}
                {g.message || g.occurrences[0]?.detail}
                {g.occurrences.length > 1 && (
                  <span className="opacity-70"> · {g.occurrences.length} places</span>
                )}
              </li>
            ))}
            {warningGroups.length > 4 && (
              <li className="text-xs text-muted-foreground opacity-70">
                and {warningGroups.length - 4} more
              </li>
            )}
          </ul>
        </section>
      )}

      {/* 1 — map fields to columns */}
      {manifest && (
        <section className="rounded-xl border border-border bg-background/40 p-4 space-y-3">
          <StepHeader step={1} active={step === 1} done={mappedCount > 0}
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

      {/* 2 — generate */}
      {manifest && suggestions.length > 0 && (
        <section className="rounded-xl border border-border bg-background/40 p-4 space-y-3">
          <StepHeader step={2} active={step === 2} done={watch.job?.status === "completed"}
            title="Generate documents" hint="One document per row, checked before it is kept" />

          <div className="pl-10 space-y-2">
            <Button size="sm" onClick={generate}
                    disabled={!!busy || watch.running || !mappedCount || sheetBlocksGeneration}>
              {busy === "generate" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileText className="h-3.5 w-3.5" />}
              {watch.running ? "Generating…" : "Generate"}
            </Button>
            <p className="text-xs text-muted-foreground">
              This moves you to Documents, where the letters and the progress of the run both are.
            </p>
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
        </section>
      )}

    </div>
  );
}
