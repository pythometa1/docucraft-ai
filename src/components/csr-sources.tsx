/**
 * CSR sources (screens S3 and S4): upload the study documents the report will
 * be written from, tag each one, and watch them become retrievable.
 *
 * The tag is mandatory and per file, because it decides which sections may
 * cite that document -- a statistical method comes from the SAP, a
 * disposition count from the TLFs. Untagged uploads would only postpone the
 * question to generation time, where getting it wrong means a section citing
 * the wrong kind of evidence.
 *
 * One failed file never blocks the others: each row carries its own status,
 * its own error and its own retry.
 */

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle, CheckCircle2, FileText, Loader2, RefreshCw, Trash2, Upload, XCircle,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type { CsrDocument, CsrReadiness } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { ErrorBanner } from "@/components/error-banner";
import { PolishedEmpty, TableSkeleton } from "@/components/skeletons";
import { cn } from "@/lib/utils";
import { plainly } from "@/components/processing-banner";

export const DOC_TYPE_LABELS: Record<string, string> = {
  protocol: "Study Protocol (+ amendments)",
  sap: "Statistical Analysis Plan",
  tlf: "Tables, Listings & Figures",
  narrative: "Patient Safety Narratives",
  ib: "Investigator's Brochure",
  icf: "Informed Consent Form",
  crf: "Sample Case Report Form",
  randomization: "Randomization / blinding documentation",
  prior_csr: "Previous CSR (style reference only)",
  other: "Other supporting document",
};

const DOC_TYPE_ORDER = [
  "protocol", "sap", "tlf", "narrative", "ib", "icf", "crf", "randomization",
  "prior_csr", "other",
];

const STATUS_TONE: Record<string, string> = {
  queued: "text-muted-foreground",
  parsing: "text-info",
  chunking: "text-info",
  indexing: "text-info",
  done: "text-success",
  failed: "text-destructive",
};

const STATUS_LABEL: Record<string, string> = {
  queued: "Queued", parsing: "Reading", chunking: "Reading",
  indexing: "Preparing", done: "Ready", failed: "Failed",
};

function StatusCell({ document }: { document: CsrDocument }) {
  const busy = ["queued", "parsing", "chunking", "indexing"].includes(document.processing_status);
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs font-medium",
                        STATUS_TONE[document.processing_status] ?? "text-muted-foreground")}>
      {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
      {document.processing_status === "done" && <CheckCircle2 className="h-3.5 w-3.5" />}
      {document.processing_status === "failed" && <XCircle className="h-3.5 w-3.5" />}
      {STATUS_LABEL[document.processing_status] ?? "Processing"}
    </span>
  );
}

export function CsrSources({ csrProjectId, onReadiness }: {
  csrProjectId: string;
  onReadiness?: (readiness: CsrReadiness) => void;
}) {
  const [documents, setDocuments] = useState<CsrDocument[] | null>(null);
  const [readiness, setReadiness] = useState<CsrReadiness | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [staged, setStaged] = useState<{ file: File; doc_type: string }[]>([]);
  const [bulkType, setBulkType] = useState("protocol");
  const fileInput = useRef<HTMLInputElement>(null);

  async function load() {
    try {
      const res = await api.csrListDocuments(csrProjectId);
      setDocuments(res.items);
      setReadiness(res.readiness);
      onReadiness?.(res.readiness);
      return res;
    } catch (e: any) {
      setError(e?.message ?? String(e));
      setDocuments([]);
      return null;
    }
  }

  useEffect(() => {
    let live = true;
    api.csrListDocuments(csrProjectId)
      .then((res) => {
        if (!live) return;
        setDocuments(res.items);
        setReadiness(res.readiness);
        onReadiness?.(res.readiness);
      })
      .catch((e) => { if (live) { setError(e?.message ?? String(e)); setDocuments([]); } });
    return () => { live = false; };
  }, [csrProjectId]);

  // Poll only while something is genuinely in flight -- an idle screen should
  // not talk to the server for the rest of the afternoon.
  useEffect(() => {
    const inFlight = (documents ?? []).some((d) =>
      ["queued", "parsing", "chunking", "indexing"].includes(d.processing_status));
    if (!inFlight) return;
    let live = true;
    const timer = setInterval(async () => {
      if (!live) return;
      try {
        const res = await api.csrProcessingStatus(csrProjectId);
        if (!live) return;
        setDocuments(res.items);
        setReadiness(res.readiness);
        onReadiness?.(res.readiness);
      } catch { /* a dropped poll is not worth a toast; the next one retries */ }
    }, 2000);
    return () => { live = false; clearInterval(timer); };
  }, [documents, csrProjectId]);

  function stage(files: FileList | null) {
    if (!files) return;
    setStaged((prev) => [...prev, ...Array.from(files).map((file) => ({ file, doc_type: bulkType }))]);
  }

  async function upload() {
    if (!staged.length) return;
    setBusy("upload");
    try {
      await api.csrUploadDocuments(csrProjectId, staged);
      setStaged([]);
      if (fileInput.current) fileInput.current.value = "";
      await load();
      toast.success("Sources uploaded — process them so sections can be drafted from them.");
    } catch (e: any) {
      toast.error("Upload failed", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  async function process() {
    setBusy("process");
    try {
      const res = await api.csrProcess(csrProjectId);
      toast.info(`Processing ${res.queued} file${res.queued === 1 ? "" : "s"}…`);
      await load();
    } catch (e: any) {
      toast.error("Could not start processing", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  async function retry(document: CsrDocument) {
    setBusy(document.id);
    try {
      await api.csrRetryDocument(document.id);
      await load();
    } catch (e: any) {
      toast.error("Retry failed", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  async function remove(document: CsrDocument) {
    setBusy(document.id);
    try {
      await api.csrDeleteDocument(document.id);
      toast.success(`${document.filename} removed.`);
      await load();
    } catch (e: any) {
      toast.error("Could not remove this source", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  async function retag(document: CsrDocument, doc_type: string) {
    try {
      await api.csrRetagDocument(document.id, doc_type);
      await load();
    } catch (e: any) {
      toast.error("Could not re-tag this source", { description: plainly(e?.message ?? String(e)) });
    }
  }

  if (documents === null) return <TableSkeleton rows={4} cols={4} />;
  if (error) return <ErrorBanner title="Sources could not be loaded" message="Try again in a moment." detail={plainly(error)} />;

  const pending = documents.some((d) => ["queued", "failed"].includes(d.processing_status));

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_18rem]">
      <div className="space-y-4">
        <div className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-foreground">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            Safety narratives and listings may contain patient-level data. Upload
            de-identified sources wherever possible. Files are restricted to this project's
            members, are never used to train AI, and are purged when the CSR is deleted.
          </span>
        </div>

        <div className="rounded-xl border border-border bg-card p-4">
          <div className="mb-3 flex flex-wrap items-end gap-3">
            <div className="min-w-52 flex-1">
              <label className="mb-1.5 block text-sm font-medium">Tag files as</label>
              <select
                className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
                value={bulkType}
                onChange={(e) => setBulkType(e.target.value)}
              >
                {DOC_TYPE_ORDER.map((t) => (
                  <option key={t} value={t}>{DOC_TYPE_LABELS[t]}</option>
                ))}
              </select>
            </div>
            <Button variant="outline" onClick={() => fileInput.current?.click()}>
              <Upload className="mr-1.5 h-4 w-4" /> Choose files
            </Button>
            <input
              ref={fileInput} type="file" multiple hidden
              accept=".pdf,.docx,.rtf,.xlsx,.csv,.txt,.md"
              onChange={(e) => stage(e.target.files)}
            />
          </div>

          {staged.length > 0 && (
            <div className="space-y-2">
              <ul className="divide-y divide-border/60 rounded-lg border border-border">
                {staged.map((entry, index) => (
                  <li key={index} className="flex items-center gap-3 px-3 py-2 text-sm">
                    <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate">{entry.file.name}</span>
                    <select
                      className="h-8 rounded-md border border-input bg-transparent px-2 text-xs"
                      value={entry.doc_type}
                      onChange={(e) => setStaged((prev) => prev.map((s, i) =>
                        (i === index ? { ...s, doc_type: e.target.value } : s)))}
                    >
                      {DOC_TYPE_ORDER.map((t) => (
                        <option key={t} value={t}>{DOC_TYPE_LABELS[t]}</option>
                      ))}
                    </select>
                    <button
                      onClick={() => setStaged((prev) => prev.filter((_, i) => i !== index))}
                      className="rounded p-1 text-muted-foreground hover:text-destructive"
                      title="Remove from this upload"
                    >
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
            subtitle="Upload the protocol, the SAP and the statistical outputs — every section of the report is written from these files and cites them."
          />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-muted/40 text-left text-muted-foreground">
                  <th className="px-4 py-2.5 font-medium">File</th>
                  <th className="px-4 py-2.5 font-medium">Type</th>
                  <th className="px-4 py-2.5 font-medium">Status</th>
                  <th className="px-4 py-2.5 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {documents.map((document) => (
                  <tr key={document.id} className="border-b border-border/60 last:border-0">
                    <td className="px-4 py-2.5">
                      <div className="font-medium text-foreground">{document.filename}</div>
                      <div className="text-xs text-muted-foreground">
                        {(document.size_bytes / (1024 * 1024)).toFixed(1)} MB
                        {document.page_count ? ` · ${document.page_count} pages` : ""}
                      </div>
                      {document.error_message && (
                        <div className="mt-1 text-xs text-destructive">{document.error_message}</div>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      <select
                        className="h-8 rounded-md border border-input bg-transparent px-2 text-xs"
                        value={document.doc_type}
                        onChange={(e) => retag(document, e.target.value)}
                      >
                        {DOC_TYPE_ORDER.map((t) => (
                          <option key={t} value={t}>{DOC_TYPE_LABELS[t]}</option>
                        ))}
                      </select>
                    </td>
                    <td className="px-4 py-2.5"><StatusCell document={document} /></td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center justify-end gap-1">
                        {document.processing_status === "failed" && (
                          <button
                            onClick={() => retry(document)}
                            disabled={busy !== null}
                            className="rounded p-1.5 text-muted-foreground hover:bg-accent"
                            title="Retry this file"
                          >
                            <RefreshCw className="h-4 w-4" />
                          </button>
                        )}
                        <button
                          onClick={() => remove(document)}
                          disabled={busy !== null}
                          className="rounded p-1.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                          title="Remove this source"
                        >
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
        {readiness && (
          <>
            <div>
              <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">Required</div>
              <ul className="space-y-1">
                {readiness.required.map((entry) => (
                  <li key={entry.doc_type} className="flex items-center gap-2 text-xs">
                    {entry.indexed
                      ? <CheckCircle2 className="h-3.5 w-3.5 text-success" />
                      : <XCircle className={cn("h-3.5 w-3.5", entry.uploaded ? "text-warning" : "text-muted-foreground")} />}
                    <span className={entry.indexed ? "text-foreground" : "text-muted-foreground"}>
                      {DOC_TYPE_LABELS[entry.doc_type]}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">Recommended</div>
              <ul className="space-y-1">
                {readiness.recommended.map((entry) => (
                  <li key={entry.doc_type} className="flex items-center gap-2 text-xs">
                    {entry.indexed
                      ? <CheckCircle2 className="h-3.5 w-3.5 text-success" />
                      : <XCircle className={cn("h-3.5 w-3.5", entry.uploaded ? "text-warning" : "text-muted-foreground")} />}
                    <span className={entry.indexed ? "text-foreground" : "text-muted-foreground"}>
                      {DOC_TYPE_LABELS[entry.doc_type]}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
            <p className="border-t border-border pt-2 text-xs text-muted-foreground">
              {readiness.ready_to_generate
                ? "Every required source is processed — sections can be drafted."
                : `Still missing: ${readiness.missing_required.map((t) => DOC_TYPE_LABELS[t]).join(", ")}.`}
            </p>
          </>
        )}
      </aside>
    </div>
  );
}
