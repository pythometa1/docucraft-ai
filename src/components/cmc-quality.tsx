/**
 * QC and export: what stands between a dossier and a submission.
 *
 * Findings are grouped by what they cost. A blocker is something that would
 * put a wrong claim in front of an assessor -- an out-of-specification result
 * nobody acknowledged, a limit that reads two ways in two sections, a value
 * no person has checked. A warning is something a reviewer should see and may
 * accept. The export gate reads the blockers, and overriding it is a decision
 * with the person's name on it rather than a checkbox that quietly succeeds.
 */

import { useEffect, useState } from "react";
import {
  AlertOctagon, AlertTriangle, CheckCircle2, Download, FileArchive,
  FileText, Info, Loader2, RefreshCw,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type { CmcExportRecord, CmcFinding } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { ErrorBanner } from "@/components/error-banner";
import { StageSkeleton } from "@/components/skeletons";
import { cn } from "@/lib/utils";

const SEVERITY: Record<string, { label: string; tone: string; Icon: typeof AlertOctagon }> = {
  blocker: { label: "Blockers", tone: "border-destructive/40 bg-destructive/10 text-destructive", Icon: AlertOctagon },
  warning: { label: "Warnings", tone: "border-warning/40 bg-warning/10 text-warning", Icon: AlertTriangle },
  info: { label: "Information", tone: "border-border bg-muted text-muted-foreground", Icon: Info },
};

function FindingRow({ finding }: { finding: CmcFinding }) {
  const tone = SEVERITY[finding.severity] ?? SEVERITY.info;
  return (
    <li className={cn("rounded-lg border px-3 py-2 text-sm", tone.tone)}>
      <div className="flex items-start gap-2">
        <tone.Icon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <div className="min-w-0">
          <div className="text-foreground">{finding.message}</div>
          <div className="mt-0.5 flex flex-wrap gap-2 text-[0.65rem] text-muted-foreground">
            <span className="font-mono">{finding.code}</span>
            {finding.section_code && <span>section {finding.section_code}</span>}
          </div>
        </div>
      </div>
    </li>
  );
}

export function CmcQuality({ cmcProjectId }: { cmcProjectId: string }) {
  const [findings, setFindings] = useState<CmcFinding[] | null>(null);
  const [exportable, setExportable] = useState(false);
  const [exports, setExports] = useState<CmcExportRecord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [granularity, setGranularity] = useState("combined");
  const [citations, setCitations] = useState("inline");
  const [watermark, setWatermark] = useState(false);

  async function load() {
    setError(null);
    try {
      const [qc, history] = await Promise.all([
        api.cmcQc(cmcProjectId),
        api.cmcListExports(cmcProjectId),
      ]);
      setFindings(qc.findings);
      setExportable(qc.exportable);
      setExports(history.items);
    } catch (e: any) {
      setError(e?.message ?? String(e));
      setFindings([]);
    }
  }

  useEffect(() => {
    let live = true;
    (async () => { if (live) await load(); })();
    return () => { live = false; };
  }, [cmcProjectId]);

  async function runExport(override: boolean) {
    if (override && !window.confirm(
      "Export with blockers outstanding? The override is recorded in the audit log with your "
      + "name, and the document may contain claims nobody has checked.")) return;
    setBusy("export");
    try {
      const record = await api.cmcExport(cmcProjectId, {
        granularity, citations, draft_watermark: watermark, override_approval: override,
      });
      toast.success(`Exported ${record.files.length} file${record.files.length === 1 ? "" : "s"}.`);
      await load();
    } catch (e: any) {
      toast.error("The export was refused", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function download(record: CmcExportRecord) {
    setBusy(record.id);
    try {
      const url = await api.cmcDownloadExport(record.id);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = record.files[0]?.filename ?? "dossier.docx";
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (e: any) {
      toast.error("Download failed", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  if (findings === null) return <StageSkeleton lines={6} />;
  if (error) return <ErrorBanner title="The checks could not be run" message="Try again in a moment." detail={error} />;

  const grouped = {
    blocker: findings.filter((f) => f.severity === "blocker"),
    warning: findings.filter((f) => f.severity === "warning"),
    info: findings.filter((f) => f.severity === "info"),
  };

  return (
    <div className="space-y-4">
      <div className={cn(
        "flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3",
        exportable ? "border-success/40 bg-success/10" : "border-destructive/40 bg-destructive/10",
      )}>
        <div className="flex items-center gap-2 text-sm">
          {exportable
            ? <CheckCircle2 className="h-4 w-4 text-success" />
            : <AlertOctagon className="h-4 w-4 text-destructive" />}
          <span className="font-medium text-foreground">
            {exportable
              ? "Nothing is blocking an export."
              : `${grouped.blocker.length} blocker${grouped.blocker.length === 1 ? "" : "s"}`}
          </span>
          <span className="text-muted-foreground">
            · {grouped.warning.length} warning{grouped.warning.length === 1 ? "" : "s"}
          </span>
        </div>
        <Button size="sm" variant="outline" onClick={load} disabled={busy !== null}>
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" /> Run checks again
        </Button>
      </div>

      {(["blocker", "warning", "info"] as const).map((severity) => (
        grouped[severity].length > 0 && (
          <div key={severity} className="space-y-2">
            <h3 className="text-sm font-semibold text-foreground">
              {SEVERITY[severity].label}
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                {grouped[severity].length}
              </span>
            </h3>
            <ul className="space-y-1.5">
              {grouped[severity].map((finding, index) => (
                <FindingRow key={`${finding.code}-${index}`} finding={finding} />
              ))}
            </ul>
          </div>
        )
      ))}

      {findings.length === 0 && (
        <p className="rounded-lg border border-border bg-muted/30 px-4 py-6 text-center text-sm text-muted-foreground">
          No findings. Add sources and draft some sections, then run the checks again.
        </p>
      )}

      <div className="space-y-3 rounded-xl border border-border bg-card p-4">
        <h3 className="text-sm font-semibold text-foreground">Export</h3>
        <div className="grid gap-3 sm:grid-cols-3">
          <div>
            <label className="mb-1.5 block text-xs font-medium">Granularity</label>
            <select className="h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                    value={granularity} onChange={(e) => setGranularity(e.target.value)}>
              <option value="combined">One combined document</option>
              <option value="ectd_leaves">eCTD leaf files (zip)</option>
              <option value="both">Both</option>
            </select>
          </div>
          <div>
            <label className="mb-1.5 block text-xs font-medium">Citations</label>
            <select className="h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                    value={citations} onChange={(e) => setCitations(e.target.value)}>
              <option value="inline">Keep inline</option>
              <option value="stripped">Strip</option>
            </select>
          </div>
          <div className="flex items-end">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={watermark}
                     onChange={(e) => setWatermark(e.target.checked)} />
              Mark as DRAFT
            </label>
          </div>
        </div>
        <p className="text-xs text-muted-foreground">
          Tables are rendered from verified data at the moment of export, so a value corrected in
          Data review reaches every deliverable without a section being regenerated. An eCTD
          backbone is deliberately not produced — leaf files and a manifest are, and assembling
          the submission belongs to your publishing tool.
        </p>
        <div className="flex flex-wrap justify-end gap-2">
          {!exportable && (
            <Button variant="outline" onClick={() => runExport(true)} disabled={busy !== null}
                    className="text-destructive hover:bg-destructive/10">
              Export anyway (recorded)
            </Button>
          )}
          <Button onClick={() => runExport(false)} disabled={busy !== null || !exportable}>
            {busy === "export"
              ? <><Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> Writing…</>
              : <><FileText className="mr-1.5 h-4 w-4" /> Export</>}
          </Button>
        </div>
      </div>

      {exports.length > 0 && (
        <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Exported</th>
                <th className="px-3 py-2 font-medium">Granularity</th>
                <th className="px-3 py-2 font-medium">Files</th>
                <th className="px-3 py-2 text-right font-medium">Download</th>
              </tr>
            </thead>
            <tbody>
              {exports.map((record) => (
                <tr key={record.id} className="border-b border-border/60 last:border-0">
                  <td className="px-3 py-2 text-muted-foreground">
                    {new Date(record.created_at).toLocaleString()}
                    {record.overridden && (
                      <span className="ml-2 rounded bg-destructive/10 px-1.5 text-[0.65rem] text-destructive">
                        overridden
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 capitalize">{record.granularity.replace("_", " ")}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {record.files.map((f) => f.filename).join(", ") || "—"}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button onClick={() => download(record)} disabled={busy !== null}
                            className="rounded p-1.5 text-muted-foreground hover:bg-accent"
                            title="Download">
                      {record.granularity === "ectd_leaves"
                        ? <FileArchive className="h-4 w-4" />
                        : <Download className="h-4 w-4" />}
                    </button>
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
