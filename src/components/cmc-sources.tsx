/**
 * CMC source documents: upload them tagged, watch them index, and see what
 * the checklist still wants.
 *
 * Two tags per file rather than one. The document type decides which sections
 * may cite it; the material decides which substance or product its numbers
 * belong to. A certificate of analysis filed against the wrong material is a
 * limit applied to the wrong molecule, and that is not a mistake a draft
 * reveals -- so both are stated at upload rather than inferred later.
 *
 * The required list comes from the server, computed from the deliverables
 * this dossier actually selected: a 3.2.P needs a batch record and a 3.2.S
 * does not, and a checklist that asked for both would teach people to ignore it.
 */

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle, CheckCircle2, FileText, Loader2, RefreshCw, Trash2, Upload, XCircle,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type { CmcDocument, CmcMaterial, CmcReadiness } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { ErrorBanner } from "@/components/error-banner";
import { PolishedEmpty, TableSkeleton } from "@/components/skeletons";
import { cn } from "@/lib/utils";

export const CMC_DOC_TYPES: [string, string][] = [
  ["spec_ds", "Specification — drug substance"],
  ["spec_dp", "Specification — drug product"],
  ["spec_excipient", "Specification — excipient"],
  ["coa", "Certificate of Analysis"],
  ["stability_protocol", "Stability protocol"],
  ["stability_data", "Stability data"],
  ["method_sop", "Analytical procedure"],
  ["method_val_report", "Method validation report"],
  ["bmr", "Batch manufacturing record"],
  ["process_flow", "Process flow / narrative"],
  ["pv_report", "Process validation report"],
  ["dev_report", "Development report"],
  ["characterisation", "Characterisation / spectra"],
  ["impurity_report", "Impurity / degradation report"],
  ["ccs", "Container closure system"],
  ["ref_std", "Reference standard qualification"],
  ["site_gmp", "Site master file / GMP certificate"],
  ["dmf", "DMF / ASMF letter of access"],
  ["supplier_doc", "Supplier qualification / TSE-BSE"],
  ["deviation_capa", "Deviations / CAPA / change control"],
  ["prior_dossier", "Previously approved dossier (reference only)"],
  ["other", "Other supporting document"],
];

const DOC_LABEL = Object.fromEntries(CMC_DOC_TYPES);

/** The source types whose tables are read for values, so the screen can say
 *  which uploads will populate the data grid and which are prose only. */
const STRUCTURED = new Set(["coa", "spec_ds", "spec_dp", "spec_excipient",
                            "stability_data", "bmr"]);

const STATUS_LABEL: Record<string, string> = {
  queued: "Queued", parsing: "Parsing", chunking: "Chunking",
  extracting: "Reading values", indexing: "Indexing",
  done: "Indexed", failed: "Failed",
};

const BUSY_STATES = ["queued", "parsing", "chunking", "extracting", "indexing"];

function StatusCell({ document }: { document: CmcDocument }) {
  const busy = BUSY_STATES.includes(document.processing_status);
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs font-medium",
                        document.processing_status === "done" ? "text-success"
                          : document.processing_status === "failed" ? "text-destructive"
                            : busy ? "text-info" : "text-muted-foreground")}>
      {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
      {document.processing_status === "done" && <CheckCircle2 className="h-3.5 w-3.5" />}
      {document.processing_status === "failed" && <XCircle className="h-3.5 w-3.5" />}
      {STATUS_LABEL[document.processing_status] ?? document.processing_status}
      {document.processing_status === "done" && (
        <span className="text-muted-foreground">
          · {document.chunk_count} chunks
          {STRUCTURED.has(document.doc_type) && `, ${document.value_count} values`}
        </span>
      )}
    </span>
  );
}

export function CmcSources({ cmcProjectId, onLoaded }: {
  cmcProjectId: string;
  onLoaded?: (documents: CmcDocument[], readiness: CmcReadiness) => void;
}) {
  const [documents, setDocuments] = useState<CmcDocument[] | null>(null);
  const [readiness, setReadiness] = useState<CmcReadiness | null>(null);
  const [materials, setMaterials] = useState<CmcMaterial[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [staged, setStaged] = useState<{ file: File; doc_type: string; material_id: string }[]>([]);
  const [bulkType, setBulkType] = useState("coa");
  const fileInput = useRef<HTMLInputElement>(null);

  async function load() {
    try {
      const [docs, mats] = await Promise.all([
        api.cmcListDocuments(cmcProjectId),
        api.cmcListMaterials(cmcProjectId),
      ]);
      setDocuments(docs.items);
      setReadiness(docs.readiness);
      setMaterials(mats.items);
      onLoaded?.(docs.items, docs.readiness);
      return docs;
    } catch (e: any) {
      setError(e?.message ?? String(e));
      setDocuments([]);
      return null;
    }
  }

  useEffect(() => {
    let live = true;
    (async () => {
      const res = await load().catch(() => null);
      if (!live || !res) return;
    })();
    return () => { live = false; };
  }, [cmcProjectId]);

  // Poll only while something is genuinely in flight.
  useEffect(() => {
    const inFlight = (documents ?? []).some((d) => BUSY_STATES.includes(d.processing_status));
    if (!inFlight) return;
    let live = true;
    const timer = setInterval(async () => {
      if (!live) return;
      try {
        const res = await api.cmcProcessingStatus(cmcProjectId);
        if (!live) return;
        setDocuments(res.items);
        setReadiness(res.readiness);
        onLoaded?.(res.items, res.readiness);
      } catch { /* a dropped poll retries on the next tick */ }
    }, 2000);
    return () => { live = false; clearInterval(timer); };
  }, [documents, cmcProjectId]);

  async function upload() {
    if (!staged.length) return;
    setBusy("upload");
    try {
      await api.cmcUploadDocuments(cmcProjectId, staged.map((s) => ({
        file: s.file, doc_type: s.doc_type,
        material_id: s.material_id || undefined,
      })));
      setStaged([]);
      if (fileInput.current) fileInput.current.value = "";
      await load();
      toast.success("Uploaded — process them to index and read their values.");
    } catch (e: any) {
      toast.error("Upload failed", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function process() {
    setBusy("process");
    try {
      const res = await api.cmcProcess(cmcProjectId);
      toast.info(`Processing ${res.queued} file${res.queued === 1 ? "" : "s"}…`);
      await load();
    } catch (e: any) {
      toast.error("Could not start processing", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  if (documents === null) return <TableSkeleton rows={4} cols={4} />;
  if (error) return <ErrorBanner title="Sources could not be loaded" message="Try again in a moment." detail={error} />;

  const pending = documents.some((d) => ["queued", "failed"].includes(d.processing_status));

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_17rem]">
      <div className="space-y-4">
        <div className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-foreground">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            CMC sources contain trade secrets and confidential business information. Files are
            restricted to this project's members, are never used for model training, and are
            purged with the dossier.
          </span>
        </div>

        <div className="rounded-xl border border-border bg-card p-4">
          <div className="mb-3 flex flex-wrap items-end gap-3">
            <div className="min-w-56 flex-1">
              <label className="mb-1.5 block text-sm font-medium">Tag files as</label>
              <select className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
                      value={bulkType} onChange={(e) => setBulkType(e.target.value)}>
                {CMC_DOC_TYPES.map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </div>
            <Button variant="outline" onClick={() => fileInput.current?.click()}>
              <Upload className="mr-1.5 h-4 w-4" /> Choose files
            </Button>
            <input ref={fileInput} type="file" multiple hidden
                   accept=".pdf,.docx,.rtf,.xlsx,.csv,.txt,.md"
                   onChange={(e) => {
                     const chosen = Array.from(e.target.files ?? []);
                     setStaged((prev) => [...prev, ...chosen.map((file) => ({
                       file, doc_type: bulkType, material_id: "",
                     }))]);
                   }} />
          </div>

          {staged.length > 0 && (
            <div className="space-y-2">
              <ul className="divide-y divide-border/60 rounded-lg border border-border">
                {staged.map((entry, index) => (
                  <li key={index} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
                    <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate">{entry.file.name}</span>
                    <select className="h-8 rounded-md border border-input bg-transparent px-2 text-xs"
                            value={entry.doc_type}
                            onChange={(e) => setStaged((prev) => prev.map((s, i) =>
                              (i === index ? { ...s, doc_type: e.target.value } : s)))}>
                      {CMC_DOC_TYPES.map(([value, label]) => (
                        <option key={value} value={value}>{label}</option>
                      ))}
                    </select>
                    {materials.length > 0 && (
                      <select className="h-8 rounded-md border border-input bg-transparent px-2 text-xs"
                              value={entry.material_id}
                              onChange={(e) => setStaged((prev) => prev.map((s, i) =>
                                (i === index ? { ...s, material_id: e.target.value } : s)))}>
                        <option value="">— material (optional) —</option>
                        {materials.map((m) => (
                          <option key={m.id} value={m.id}>{m.name}</option>
                        ))}
                      </select>
                    )}
                    <button onClick={() => setStaged((prev) => prev.filter((_, i) => i !== index))}
                            className="rounded p-1 text-muted-foreground hover:text-destructive"
                            title="Remove from this upload">
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </li>
                ))}
              </ul>
              <div className="flex justify-end">
                <Button onClick={upload} disabled={busy !== null}>
                  {busy === "upload" ? "Uploading…" : `Upload ${staged.length} file${staged.length === 1 ? "" : "s"}`}
                </Button>
              </div>
            </div>
          )}
        </div>

        {documents.length === 0 ? (
          <PolishedEmpty
            icon={<FileText className="h-8 w-8 text-muted-foreground" />}
            title="No source documents yet"
            subtitle="Upload the specification, the certificates of analysis and the stability data — every number in the dossier comes from these files and is checked against them."
          />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                  <th className="px-3 py-2 font-medium">File</th>
                  <th className="px-3 py-2 font-medium">Type</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {documents.map((document) => (
                  <tr key={document.id} className="border-b border-border/60 last:border-0">
                    <td className="px-3 py-2">
                      <div className="font-medium text-foreground">{document.filename}</div>
                      <div className="text-xs text-muted-foreground">
                        {(document.size_bytes / (1024 * 1024)).toFixed(1)} MB
                        {document.page_count ? ` · ${document.page_count} pages` : ""}
                      </div>
                      {document.error_message && (
                        <div className="mt-1 text-xs text-destructive">{document.error_message}</div>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <select className="h-8 max-w-52 rounded-md border border-input bg-transparent px-2 text-xs"
                              value={document.doc_type}
                              onChange={async (e) => {
                                await api.cmcRetagDocument(document.id, { doc_type: e.target.value })
                                  .catch((err: any) => toast.error("Could not re-tag",
                                                                   { description: err?.message }));
                                await load();
                              }}>
                        {CMC_DOC_TYPES.map(([value, label]) => (
                          <option key={value} value={value}>{label}</option>
                        ))}
                      </select>
                    </td>
                    <td className="px-3 py-2"><StatusCell document={document} /></td>
                    <td className="px-3 py-2">
                      <div className="flex items-center justify-end gap-1">
                        {document.processing_status === "failed" && (
                          <button onClick={async () => {
                            await api.cmcRetryDocument(document.id).catch(() => undefined);
                            await load();
                          }} className="rounded p-1.5 text-muted-foreground hover:bg-accent"
                                  title="Retry this file">
                            <RefreshCw className="h-4 w-4" />
                          </button>
                        )}
                        <button onClick={async () => {
                          const res = await api.cmcDeleteDocument(document.id).catch(() => null);
                          if (res) {
                            toast.success(
                              `Removed. ${res.purged_values} unverified value${res.purged_values === 1 ? "" : "s"} went with it`
                              + (res.kept_verified_values
                                ? `; ${res.kept_verified_values} verified value${res.kept_verified_values === 1 ? "" : "s"} kept.`
                                : "."));
                          }
                          await load();
                        }} className="rounded p-1.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                                title="Remove this source and the values read from it">
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

        {pending && (
          <div className="flex justify-end">
            <Button onClick={process} disabled={busy !== null}>
              {busy === "process" ? "Starting…" : "Process documents"}
            </Button>
          </div>
        )}
      </div>

      <aside className="space-y-3 rounded-xl border border-border bg-card p-4 text-sm">
        <h3 className="font-semibold text-foreground">Source checklist</h3>
        {readiness === null ? <TableSkeleton rows={3} cols={1} /> : (
          <>
            {(["required", "recommended"] as const).map((group) => (
              <div key={group}>
                <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  {group}
                </div>
                {readiness[group].length === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    {group === "required"
                      ? "Add a deliverable and its required sources appear here."
                      : "None."}
                  </p>
                ) : (
                  <ul className="space-y-1">
                    {readiness[group].map((entry) => (
                      <li key={entry.doc_type} className="flex items-start gap-2 text-xs">
                        {entry.indexed
                          ? <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-success" />
                          : <XCircle className={cn("mt-0.5 h-3.5 w-3.5 shrink-0",
                                                   entry.uploaded ? "text-warning" : "text-muted-foreground")} />}
                        <span className={entry.indexed ? "text-foreground" : "text-muted-foreground"}>
                          {DOC_LABEL[entry.doc_type] ?? entry.doc_type}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
            <p className="border-t border-border pt-2 text-xs text-muted-foreground">
              {readiness.ready_to_generate
                ? "Every required source is indexed. Check its values in Data review before drafting."
                : readiness.missing_required.length
                  ? `Still missing: ${readiness.missing_required.map((t) => DOC_LABEL[t] ?? t).join(", ")}.`
                  : "Add a deliverable to see what this dossier needs."}
            </p>
          </>
        )}
      </aside>
    </div>
  );
}
