/**
 * S3 and S4: getting safety sources in, and watching where they stop.
 *
 * The screen has to make two distinctions the other modules do not.
 *
 * **Two tags per file, not one.** The document type decides which sections may
 * cite a file; the input type decides which pipeline reads it. An E2B export
 * read as a document becomes prose nobody can count, and a study report read as
 * an ICSR fails on every field — so both are asked for at upload, and the
 * server refuses a combination that cannot be right.
 *
 * **A parsed source is not a finished source.** Masking runs before anything is
 * indexed, embedded or sent to a model, and a detection the machine will not
 * settle stops the file at `awaiting_deid` with no chunks at all — rather than
 * chunks that are mostly masked. The banner says which files are held and why.
 */
import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle, CheckCircle2, FileText, Loader2, Lock, RefreshCw, Table2,
  Trash2, Upload, XCircle,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type {
  PvCaseRow, PvDeidGate, PvDeidItem, PvMappingProfile, PvReadiness, PvSource,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorBanner } from "@/components/error-banner";
import { PolishedEmpty, TableSkeleton } from "@/components/skeletons";
import { cn } from "@/lib/utils";
import { plainly } from "@/components/processing-banner";

const SELECT_CLASS =
  "h-8 rounded-md border border-input bg-transparent px-2 text-xs";

const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  parsing: "Reading",
  deidentifying: "Hiding personal details",
  awaiting_deid: "Held — personal details need an answer",
  indexing: "Preparing",
  done: "Done",
  failed: "Failed",
};

/** Every state the worker passes through. A screen that stopped polling at
 *  `deidentifying` would show a half-finished pipeline as a finished one. */
const BUSY_STATES = new Set(["queued", "parsing", "deidentifying", "indexing"]);

type Staged = { file: File; doc_type: string; input_type: string };

/** Guessed from the extension, because it is right almost every time and the
 *  server refuses it when it is not. */
function guessInput(name: string): string {
  const lower = name.toLowerCase();
  if (lower.endsWith(".xml")) return "e2b_r3_xml";
  if (lower.endsWith(".csv") || lower.endsWith(".xlsx")) return "line_listing";
  return "document";
}

export function SafetySources({ productId, onCases }: {
  productId: string;
  onCases?: (count: number) => void;
}) {
  const [sources, setSources] = useState<PvSource[] | null>(null);
  const [readiness, setReadiness] = useState<PvReadiness | null>(null);
  const [gate, setGate] = useState<PvDeidGate | null>(null);
  const [docTypes, setDocTypes] = useState<Record<string, string>>({});
  const [inputTypes, setInputTypes] = useState<Record<string, string>>({});
  const [mappings, setMappings] = useState<Record<string, Record<string, string>>>({});
  const [staged, setStaged] = useState<Staged[]>([]);
  const [mapping, setMapping] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  async function load() {
    try {
      const res = await api.pvSources(productId);
      setSources(res.items);
      setReadiness(res.readiness);
      setDocTypes(res.doc_types);
      setInputTypes(res.input_types);
      setError(null);
    } catch (e: any) {
      setError(e?.message ?? String(e));
      setSources([]);
    }
  }

  useEffect(() => {
    let live = true;
    api.pvSources(productId)
      .then((res) => {
        if (!live) return;
        setSources(res.items);
        setReadiness(res.readiness);
        setDocTypes(res.doc_types);
        setInputTypes(res.input_types);
      })
      .catch((e: any) => {
        if (live) { setError(e?.message ?? String(e)); setSources([]); }
      });
    return () => { live = false; };
  }, [productId]);

  // Polled only while something is actually moving. A screen that polls when
  // nothing is happening is a screen that keeps a database busy for no reason.
  const inFlight = (sources ?? []).some((s) => BUSY_STATES.has(s.processing_status));
  useEffect(() => {
    if (!inFlight) return;
    let live = true;
    const timer = setInterval(() => {
      api.pvProcessingStatus(productId)
        .then((res) => {
          if (!live) return;
          setSources(res.items);
          setGate(res.deid_gate);
          onCases?.(res.cases);
        })
        .catch(() => { /* the next tick tries again */ });
    }, 1500);
    return () => { live = false; clearInterval(timer); };
  }, [productId, inFlight, onCases]);

  // The gate is read once on arrival too, so it is right before anything runs.
  useEffect(() => {
    let live = true;
    api.pvProcessingStatus(productId)
      .then((res) => { if (live) { setGate(res.deid_gate); onCases?.(res.cases); } })
      .catch(() => { /* the banner simply does not show */ });
    return () => { live = false; };
  }, [productId, sources, onCases]);

  function stage(files: FileList | null) {
    if (!files) return;
    setStaged((current) => [
      ...current,
      ...Array.from(files).map((file) => ({
        file, doc_type: "other", input_type: guessInput(file.name),
      })),
    ]);
    if (fileInput.current) fileInput.current.value = "";
  }

  async function upload() {
    if (!staged.length) return;
    setBusy("upload");
    try {
      await api.pvUploadSources(productId, staged);
      setStaged([]);
      await load();
      toast.success(`${staged.length} source(s) uploaded.`);
    } catch (e: any) {
      toast.error("Upload failed", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  async function process() {
    setBusy("process");
    try {
      const res = await api.pvProcessSources(productId, mappings);
      await load();
      toast.success(`${res.queued} source(s) queued.`);
    } catch (e: any) {
      toast.error("Processing could not start", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  async function remove(source: PvSource) {
    if (!window.confirm(
      `Delete ${source.filename}? Every case read out of it goes too — a case ` +
      "whose source is gone cannot be traced, and it would still be counted."
    )) return;
    setBusy(source.id);
    try {
      const res = await api.pvDeleteSource(source.id);
      await load();
      toast.success(`${source.filename} removed`,
                    res.purged.cases
                      ? { description: `${res.purged.cases} case(s) went with it.` }
                      : undefined);
    } catch (e: any) {
      toast.error("The source could not be removed",
                  { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  async function retag(source: PvSource, patch: Record<string, string>) {
    setBusy(source.id);
    try {
      await api.pvRetagSource(source.id, patch);
      await load();
    } catch (e: any) {
      toast.error("The tag could not be changed",
                  { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  if (sources === null) return <TableSkeleton rows={4} cols={5} />;
  if (error) {
    return <ErrorBanner title="Sources could not be loaded"
                        message="Try again in a moment." detail={plainly(error)} />;
  }

  const needMapping = sources.filter(
    (s) => s.input_type === "line_listing" && !mappings[s.id]
           && s.processing_status !== "awaiting_deid");

  return (
    <div className="space-y-4">
      {gate && !gate.cleared && (
        <div className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
          <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
          <div>
            <span className="font-medium text-foreground">
              {gate.documents_waiting} source(s) and {gate.cases_pending} case(s) are
              waiting for personal details to be checked.
            </span>{" "}
            <span className="text-muted-foreground">{plainly(gate.note ?? "")}</span>
          </div>
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-[1fr_17rem]">
        <div className="space-y-3">
          <div className="rounded-xl border border-dashed border-border p-4">
            <div className="flex flex-wrap items-center gap-2">
              <input ref={fileInput} type="file" multiple className="hidden"
                     onChange={(e) => stage(e.target.files)} />
              <Button variant="outline" onClick={() => fileInput.current?.click()}>
                <Upload className="mr-1.5 h-4 w-4" /> Choose files
              </Button>
              <span className="text-xs text-muted-foreground">
                E2B XML, line listings (.csv/.xlsx), CIOMS forms and supporting documents.
              </span>
            </div>

            {staged.length > 0 && (
              <div className="mt-3 space-y-2">
                {staged.map((entry, index) => (
                  <div key={`${entry.file.name}-${index}`}
                       className="flex flex-wrap items-center gap-2 rounded-lg border border-border px-2 py-1.5">
                    <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate text-xs">{entry.file.name}</span>
                    <select className={SELECT_CLASS} value={entry.input_type}
                            onChange={(e) => setStaged((s) => s.map((x, i) =>
                              i === index ? { ...x, input_type: e.target.value } : x))}>
                      {Object.entries(inputTypes).map(([key, label]) => (
                        <option key={key} value={key}>{label}</option>
                      ))}
                    </select>
                    <select className={SELECT_CLASS} value={entry.doc_type}
                            onChange={(e) => setStaged((s) => s.map((x, i) =>
                              i === index ? { ...x, doc_type: e.target.value } : x))}>
                      {Object.entries(docTypes).map(([key, label]) => (
                        <option key={key} value={key}>{label}</option>
                      ))}
                    </select>
                    <button onClick={() => setStaged((s) => s.filter((_x, i) => i !== index))}
                            className="rounded p-1 text-muted-foreground hover:bg-accent">
                      <XCircle className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
                <div className="flex justify-end">
                  <Button onClick={upload} disabled={busy !== null}>
                    {busy === "upload" ? "Uploading…" : `Upload ${staged.length} file(s)`}
                  </Button>
                </div>
              </div>
            )}
          </div>

          {sources.length === 0 ? (
            <PolishedEmpty
              icon={<Upload className="h-8 w-8 text-muted-foreground" />}
              title="No sources yet"
              subtitle="An E2B export or a line listing becomes your case data. Supporting documents are what the written sections will cite."
            />
          ) : (
            <div className="overflow-x-auto rounded-xl border border-border">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                    <th className="px-3 py-2 font-medium">File</th>
                    <th className="px-3 py-2 font-medium">Read as</th>
                    <th className="px-3 py-2 font-medium">Document type</th>
                    <th className="px-3 py-2 font-medium">Cases</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {sources.map((source) => (
                    <tr key={source.id} className="border-b border-border/60 last:border-0">
                      <td className="max-w-[16rem] px-3 py-1.5">
                        <div className="truncate font-medium text-foreground">
                          {source.filename}
                        </div>
                        {source.error_message && (
                          <div className="mt-0.5 text-xs text-destructive">
                            {source.error_message}
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-1.5">
                        <select className={SELECT_CLASS} value={source.input_type}
                                disabled={busy !== null}
                                onChange={(e) => retag(source, { input_type: e.target.value })}>
                          {Object.entries(inputTypes).map(([key, label]) => (
                            <option key={key} value={key}>{label}</option>
                          ))}
                        </select>
                      </td>
                      <td className="px-3 py-1.5">
                        <select className={SELECT_CLASS} value={source.doc_type}
                                disabled={busy !== null}
                                onChange={(e) => retag(source, { doc_type: e.target.value })}>
                          {Object.entries(docTypes).map(([key, label]) => (
                            <option key={key} value={key}>{label}</option>
                          ))}
                        </select>
                      </td>
                      <td className="px-3 py-1.5 text-muted-foreground">
                        {source.case_count || "—"}
                      </td>
                      <td className="px-3 py-1.5">
                        <StatusCell source={source} />
                      </td>
                      <td className="px-3 py-1.5 text-right">
                        <div className="flex justify-end gap-1">
                          {source.input_type === "line_listing" && (
                            <button onClick={() => setMapping(source.id)}
                                    className={cn(
                                      "rounded p-1.5 hover:bg-accent",
                                      mappings[source.id]
                                        ? "text-success" : "text-warning")}
                                    title={mappings[source.id]
                                      ? "Columns mapped — click to change"
                                      : "Map the columns before processing"}>
                              <Table2 className="h-4 w-4" />
                            </button>
                          )}
                          <button onClick={() => remove(source)} disabled={busy !== null}
                                  className="rounded p-1.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive">
                            <Trash2 className="h-4 w-4" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {sources.length > 0 && (
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-xs text-muted-foreground">
                {needMapping.length > 0
                  ? `${needMapping.length} line listing(s) need their columns mapped first.`
                  : "Cases are read from your sources, and personal details are hidden before anything is used."}
              </span>
              <Button onClick={process}
                      disabled={busy !== null || needMapping.length > 0}>
                {busy === "process"
                  ? <><Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> Starting…</>
                  : <><RefreshCw className="mr-1.5 h-4 w-4" /> Process sources</>}
              </Button>
            </div>
          )}
        </div>

        <ReadinessRail readiness={readiness} />
      </div>

      {mapping && (
        <ColumnMapper
          productId={productId}
          documentId={mapping}
          initial={mappings[mapping]}
          onClose={() => setMapping(null)}
          onSaved={(map) => {
            setMappings((m) => ({ ...m, [mapping]: map }));
            setMapping(null);
          }}
        />
      )}
    </div>
  );
}

function StatusCell({ source }: { source: PvSource }) {
  const status = source.processing_status;
  if (status === "failed") {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-destructive">
        <XCircle className="h-3.5 w-3.5" /> {STATUS_LABEL[status]}
      </span>
    );
  }
  if (BUSY_STATES.has(status)) {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> {STATUS_LABEL[status]}
      </span>
    );
  }
  if (status === "awaiting_deid") {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-warning"
            title="Read, but not used yet: personal details are hidden before anything is used.">
        <Lock className="h-3.5 w-3.5" /> {STATUS_LABEL[status]}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 text-xs text-success">
      <CheckCircle2 className="h-3.5 w-3.5" /> {STATUS_LABEL[status] ?? "Done"}
    </span>
  );
}

function ReadinessRail({ readiness }: { readiness: PvReadiness | null }) {
  if (!readiness) return null;
  const line = (entry: { doc_type: string; label: string; present: boolean }) => (
    <li key={entry.doc_type} className="flex items-start gap-1.5">
      {entry.present
        ? <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0 text-success" />
        : <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />}
      <span className={cn("text-xs", entry.present
        ? "text-foreground" : "text-muted-foreground")}>{entry.label}</span>
    </li>
  );
  return (
    <aside className="space-y-4 rounded-xl border border-border p-4">
      <div>
        <h4 className="text-sm font-medium">Required</h4>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Computed from the report types this product produces, not a fixed list.
        </p>
        <ul className="mt-2 space-y-1">
          {readiness.required.length === 0
            ? <li className="text-xs text-muted-foreground">
                Create a reporting interval to see what it needs.
              </li>
            : readiness.required.map(line)}
        </ul>
      </div>
      {readiness.recommended.length > 0 && (
        <div>
          <h4 className="text-sm font-medium">Recommended</h4>
          <ul className="mt-2 space-y-1">{readiness.recommended.map(line)}</ul>
        </div>
      )}
    </aside>
  );
}

/* ------------------------------------------------------------ column mapping */

function ColumnMapper({ productId, documentId, initial, onClose, onSaved }: {
  productId: string;
  documentId: string;
  initial?: Record<string, string>;
  onClose: () => void;
  onSaved: (map: Record<string, string>) => void;
}) {
  const [data, setData] = useState<Awaited<
    ReturnType<typeof api.pvSourceColumns>> | null>(null);
  const [map, setMap] = useState<Record<string, string>>(initial ?? {});
  const [profiles, setProfiles] = useState<PvMappingProfile[]>([]);
  const [profileName, setProfileName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    Promise.all([api.pvSourceColumns(documentId), api.pvMappingProfiles(productId)])
      .then(([columns, saved]) => {
        if (!live) return;
        setData(columns);
        setProfiles(saved.items);
        if (!initial) {
          // The suggestion is a starting point shown for confirmation, never
          // applied silently: a column mapped wrongly puts one field's values
          // under another field's name.
          const guessed: Record<string, string> = {};
          for (const s of columns.suggestions) {
            if (s.field) guessed[s.column] = s.field;
          }
          setMap(guessed);
        }
      })
      .catch((e: any) => { if (live) setError(e?.message ?? String(e)); });
    return () => { live = false; };
  }, [documentId, productId, initial]);

  async function saveProfile() {
    if (!profileName.trim()) {
      toast.error("A profile needs a name, such as the system it came from.");
      return;
    }
    setBusy(true);
    try {
      await api.pvSaveMappingProfile(productId, {
        name: profileName.trim(), column_map: map,
      });
      toast.success("Mapping saved. The next cycle is one click.");
      setProfileName("");
    } catch (e: any) {
      toast.error("The mapping could not be saved",
                  { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
         role="dialog" aria-modal="true">
      <div className="max-h-[88vh] w-full max-w-4xl overflow-y-auto rounded-2xl border border-border bg-background p-5">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h3 className="text-base font-semibold">Map the columns</h3>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Every safety system exports the same facts under different headings.
              Confirm each column, then save the mapping so the next cycle is one click.
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={onClose}>Close</Button>
        </div>

        {error && (
          <div className="mt-4">
            <ErrorBanner title="The columns could not be read"
                         message="Check the file has a header row." detail={plainly(error)} />
          </div>
        )}

        {!data ? <div className="mt-4"><TableSkeleton rows={5} cols={3} /></div> : (
          <>
            {profiles.length > 0 && (
              <div className="mt-4 flex flex-wrap items-center gap-2">
                <span className="text-xs text-muted-foreground">Saved mappings:</span>
                {profiles.map((profile) => (
                  <button key={profile.id}
                          onClick={() => setMap(profile.column_map)}
                          className="rounded-full border border-border px-2.5 py-0.5 text-xs hover:bg-accent">
                    {profile.name}
                  </button>
                ))}
              </div>
            )}

            <div className="mt-4 overflow-x-auto rounded-lg border border-border">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                    <th className="px-3 py-2 font-medium">Column</th>
                    <th className="px-3 py-2 font-medium">First value</th>
                    <th className="px-3 py-2 font-medium">Maps to</th>
                  </tr>
                </thead>
                <tbody>
                  {data.headers.map((header, index) => (
                    <tr key={`${header}-${index}`}
                        className="border-b border-border/60 last:border-0">
                      <td className="px-3 py-1.5 font-medium text-foreground">{header}</td>
                      <td className="px-3 py-1.5 font-mono text-xs text-muted-foreground">
                        {data.sample_rows[0]?.[index] ?? "—"}
                      </td>
                      <td className="px-3 py-1.5">
                        <select className={cn(SELECT_CLASS, "w-full")}
                                value={map[header] ?? ""}
                                onChange={(e) => setMap((m) => {
                                  const next = { ...m };
                                  if (e.target.value) next[header] = e.target.value;
                                  else delete next[header];
                                  return next;
                                })}>
                          <option value="">— not mapped —</option>
                          {Object.entries(data.fields).map(([key, label]) => (
                            <option key={key} value={key}>{label}</option>
                          ))}
                        </select>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="mt-4 rounded-lg border border-border bg-muted/20 p-3">
              <label className="text-xs font-medium">
                How this source writes numeric dates
              </label>
              <p className="mt-0.5 text-xs text-muted-foreground">
                03/04/2026 is two different dates, and a receipt date decides which
                reporting interval a case falls in. Rows with an ambiguous date are
                refused unless this says which order to read.
              </p>
              <select className={cn(SELECT_CLASS, "mt-2 w-64")}
                      value={map[data.date_order_key] ?? ""}
                      onChange={(e) => setMap((m) => {
                        const next = { ...m };
                        if (e.target.value) next[data.date_order_key] = e.target.value;
                        else delete next[data.date_order_key];
                        return next;
                      })}>
                <option value="">Refuse ambiguous dates (recommended)</option>
                <option value="day_first">Day first — 03/04 is 3 April</option>
                <option value="month_first">Month first — 03/04 is 4 March</option>
              </select>
            </div>

            <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Input className="h-8 w-56 text-xs" placeholder="Save as, e.g. Argus export"
                       value={profileName}
                       onChange={(e) => setProfileName(e.target.value)} />
                <Button variant="outline" size="sm" className="h-8"
                        onClick={saveProfile} disabled={busy}>
                  Save mapping
                </Button>
              </div>
              <Button onClick={() => onSaved(map)}>Use this mapping</Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------ the case store */

export function SafetyCases({ productId, reportInstanceId }: {
  productId: string;
  reportInstanceId?: string;
}) {
  const PAGE = 50;
  const [rows, setRows] = useState<PvCaseRow[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState("");
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => setQuery(filter.trim()), 300);
    return () => clearTimeout(timer);
  }, [filter]);
  useEffect(() => { setOffset(0); }, [query, reportInstanceId]);

  useEffect(() => {
    let live = true;
    api.pvCases(productId, {
      report_instance_id: reportInstanceId, q: query || undefined,
      limit: PAGE, offset,
    })
      .then((res) => { if (live) { setRows(res.items); setTotal(res.total); } })
      .catch((e: any) => { if (live) { setError(e?.message ?? String(e)); setRows([]); } });
    return () => { live = false; };
  }, [productId, reportInstanceId, query, offset]);

  if (rows === null) return <TableSkeleton rows={5} cols={6} />;
  if (error) {
    return <ErrorBanner title="The cases could not be loaded"
                        message="Try again in a moment." detail={plainly(error)} />;
  }
  if (!rows.length && !query) {
    return (
      <PolishedEmpty
        icon={<FileText className="h-8 w-8 text-muted-foreground" />}
        title="No cases yet"
        subtitle="Upload an E2B export or a line listing and process it. Cases arrive unconfirmed: nothing counts anywhere until a person confirms it."
      />
    );
  }

  const SCOPE_TONE: Record<string, string> = {
    interval: "bg-brand/15 text-brand",
    cumulative: "bg-muted text-muted-foreground",
    after_lock: "bg-warning/15 text-warning",
    undated: "bg-destructive/15 text-destructive",
    outside: "bg-muted text-muted-foreground",
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Input value={filter} onChange={(e) => setFilter(e.target.value)}
               placeholder="Search case id, country or term…"
               className="h-8 w-72 text-xs" />
        <span className="text-xs text-muted-foreground">
          {total} case(s){reportInstanceId && " · badged against the chosen interval"}
        </span>
      </div>

      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
              <th className="px-3 py-2 font-medium">Case</th>
              <th className="px-3 py-2 font-medium">Received</th>
              <th className="px-3 py-2 font-medium">Country</th>
              <th className="px-3 py-2 font-medium">Serious</th>
              <th className="px-3 py-2 font-medium">Events</th>
              {reportInstanceId && <th className="px-3 py-2 font-medium">Scope</th>}
              <th className="px-3 py-2 font-medium">State</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} className="border-b border-border/60 last:border-0">
                <td className="px-3 py-1.5">
                  <span className="font-mono text-xs font-medium text-foreground">
                    {row.worldwide_case_id ?? "—"}
                  </span>
                  {row.case_version && row.case_version > 1 && (
                    <span className="ml-1.5 text-[0.65rem] text-muted-foreground">
                      v{row.case_version}
                    </span>
                  )}
                </td>
                <td className="px-3 py-1.5 font-mono text-xs text-muted-foreground">
                  {row.initial_receipt_date ?? "—"}
                  {row.latest_receipt_date
                    && row.latest_receipt_date !== row.initial_receipt_date
                    && <span title="latest receipt"> → {row.latest_receipt_date}</span>}
                </td>
                <td className="px-3 py-1.5">{row.country_of_occurrence ?? "—"}</td>
                <td className="px-3 py-1.5">
                  {row.is_serious
                    ? <span className="text-warning">
                        {row.seriousness_criteria.join(", ").replace(/_/g, " ") || "yes"}
                      </span>
                    : <span className="text-muted-foreground">—</span>}
                </td>
                <td className="px-3 py-1.5 text-muted-foreground">
                  {row.event_count}
                  {row.coding_required > 0 && (
                    <span className="ml-1.5 rounded bg-warning/15 px-1.5 text-[0.65rem] text-warning"
                          title="Events with no MedDRA code. An uncoded event is in no tabulation.">
                      {row.coding_required} uncoded
                    </span>
                  )}
                </td>
                {reportInstanceId && (
                  <td className="px-3 py-1.5">
                    <span className={cn("rounded px-1.5 py-0.5 text-[0.65rem]",
                                        SCOPE_TONE[row.scope ?? "outside"])}>
                      {(row.scope ?? "outside").replace("_", " ")}
                    </span>
                  </td>
                )}
                <td className="px-3 py-1.5">
                  <span className="text-xs text-muted-foreground">
                    {row.confirmed_by ? "confirmed" : "unconfirmed"}
                    {row.deidentification_status === "pending" && " · not de-identified"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {total > PAGE && (
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>
            Showing {Math.min(offset + 1, total)}–{Math.min(offset + PAGE, total)} of {total}
          </span>
          <div className="flex items-center gap-1">
            <Button variant="outline" size="sm" className="h-7 px-2"
                    disabled={offset === 0}
                    onClick={() => setOffset(Math.max(0, offset - PAGE))}>
              Previous
            </Button>
            <Button variant="outline" size="sm" className="h-7 px-2"
                    disabled={offset + PAGE >= total}
                    onClick={() => setOffset(offset + PAGE)}>
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------ S4: the de-identification queue */

/**
 * The gate, as a screen.
 *
 * Every row is one string the masking pass found and would not settle: a
 * capitalised pair might be a person and might be a diagnosis, and guessing
 * either way is silent — one leaks a name, the other destroys the clinical
 * fact the case exists to record. So the question is asked once per string per
 * product, with the reason it was uncertain, and answering it releases every
 * source waiting on the same answer.
 */
export function SafetyDeidQueue({ productId, onCleared }: {
  productId: string;
  onCleared?: () => void;
}) {
  const [items, setItems] = useState<PvDeidItem[] | null>(null);
  const [types, setTypes] = useState<string[]>([]);
  const [waiting, setWaiting] = useState(0);
  const [cleared, setCleared] = useState(false);
  const [scan, setScan] = useState<{ clean: boolean; findings: unknown[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let live = true;
    api.pvDeidQueue(productId)
      .then((res) => {
        if (!live) return;
        setItems(res.items);
        setTypes(res.identifier_types);
        setWaiting(res.documents_waiting);
        setCleared(res.cleared);
        if (res.cleared) onCleared?.();
      })
      .catch((e: any) => { if (live) { setError(e?.message ?? String(e)); setItems([]); } });
    return () => { live = false; };
  }, [productId, reload, onCleared]);

  async function resolve(item: PvDeidItem, action: "mask" | "not_an_identifier",
                         identifierType?: string) {
    setBusy(item.id);
    try {
      const res = await api.pvResolveDeidItem(item.id, {
        action, identifier_type: identifierType,
      });
      setReload((n) => n + 1);
      if (res.documents_indexed) {
        toast.success(`${res.documents_indexed} source(s) released and ready to use.`);
      }
    } catch (e: any) {
      toast.error("The answer could not be recorded",
                  { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  async function override() {
    const reason = window.prompt(
      "Releasing these sources without answering lets unchecked personal details "
      + "be used. This is recorded against your name. Why?");
    if (!reason?.trim()) return;
    setBusy("override");
    try {
      const res = await api.pvOverrideDeidQueue(productId, reason.trim());
      setReload((n) => n + 1);
      toast.warning(`${res.overridden} possible personal detail(s) left visible.`,
                    { description: `${res.documents_indexed} source(s) released. Recorded.` });
    } catch (e: any) {
      toast.error("The sources could not be released",
                  { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  async function runScan() {
    setBusy("scan");
    try {
      setScan(await api.pvLeakageScan(productId));
    } catch (e: any) {
      toast.error("The scan could not run", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  if (items === null) return <TableSkeleton rows={4} cols={3} />;
  if (error) {
    return <ErrorBanner title="The queue could not be loaded"
                        message="Try again in a moment." detail={plainly(error)} />;
  }

  return (
    <div className="space-y-4">
      <div className={cn(
        "flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3",
        cleared ? "border-success/40 bg-success/10" : "border-warning/40 bg-warning/10")}>
        <div className="flex items-center gap-2 text-sm">
          {cleared ? <CheckCircle2 className="h-4 w-4 text-success" />
                   : <Lock className="h-4 w-4 text-warning" />}
          <span className="font-medium text-foreground">
            {cleared
              ? "Nothing is waiting. Personal details are hidden and your sources are ready."
              : `${items.length} item(s) to answer · ${waiting} source(s) held`}
          </span>
          {!cleared && (
            <span className="text-muted-foreground">
              — these sources are not used for anything until you answer.
            </span>
          )}
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={runScan} disabled={busy !== null}>
            Scan for leaks
          </Button>
          {!cleared && (
            <Button variant="outline" size="sm" onClick={override}
                    disabled={busy !== null}
                    className="text-destructive hover:bg-destructive/10">
              Override (recorded)
            </Button>
          )}
        </div>
      </div>

      {scan && (
        <div className={cn(
          "rounded-lg border px-3 py-2 text-xs",
          scan.clean ? "border-success/40 bg-success/10" : "border-destructive/40 bg-destructive/10")}>
          {scan.clean
            ? "No personal details found in your processed sources."
            : `${scan.findings.length} identifier(s) found in text that should be clean.`}
        </div>
      )}

      {items.length === 0 ? (
        <PolishedEmpty
          icon={<CheckCircle2 className="h-8 w-8 text-success" />}
          title="Nothing to review"
          subtitle="Personal details we were sure of are already hidden. Anything we were not sure of would appear here for you to decide."
        />
      ) : (
        <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Found</th>
                <th className="px-3 py-2 font-medium">Why it is uncertain</th>
                <th className="px-3 py-2 text-right font-medium">Answer</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} className="border-b border-border/60 last:border-0">
                  <td className="px-3 py-2 font-medium text-foreground">
                    {item.detected_text}
                  </td>
                  <td className="max-w-md px-3 py-2 text-xs text-muted-foreground">
                    {item.context_snippet}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap justify-end gap-1.5">
                      <select className={SELECT_CLASS}
                              defaultValue={item.proposed_mask ?? "patient_name"}
                              id={`type-${item.id}`}>
                        {types.map((kind) => (
                          <option key={kind} value={kind}>
                            {kind.replace(/_/g, " ")}
                          </option>
                        ))}
                      </select>
                      <Button size="sm" className="h-8" disabled={busy !== null}
                              onClick={() => resolve(
                                item, "mask",
                                (document.getElementById(`type-${item.id}`) as
                                  HTMLSelectElement | null)?.value)}>
                        Hide it
                      </Button>
                      <Button size="sm" variant="outline" className="h-8"
                              disabled={busy !== null}
                              onClick={() => resolve(item, "not_an_identifier")}>
                        Not an identifier
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
