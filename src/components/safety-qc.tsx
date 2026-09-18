/**
 * S9 and S10: everything between a periodic safety report and the file that
 * leaves the building — the checks, the signature, the export, the record.
 *
 * The verdict at the top is not a preview of the export gate — it IS the gate.
 * The server computes "exportable" from exactly the list below, with the same
 * function the export endpoint calls, so this screen and the export cannot
 * disagree. Approval and sign-off are findings here for the same reason.
 *
 * Every finding says what is wrong and where. A check that could not run is
 * shown as a blocker naming the check: it has not passed. A handful of
 * blockers read prose with a pattern, and only those can be accepted — by a
 * qualified person, with a reason that stays on the record.
 */
import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle, CheckCircle2, Download, FileSignature, History, Info, OctagonX,
  RefreshCw, ShieldCheck, Undo2,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type {
  PvAuditEntry, PvExportOptions, PvExportRecord, PvFinding, PvQcReport,
  PvReportInstance,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorBanner } from "@/components/error-banner";
import { PolishedEmpty, TableSkeleton } from "@/components/skeletons";
import { cn } from "@/lib/utils";

const SELECT_CLASS =
  "h-8 rounded-md border border-input bg-transparent px-2 text-xs";

const APPENDICES: [string, string][] = [
  ["line_listings", "Line listings"],
  ["tabulations", "Summary tabulations"],
  ["rsi", "Reference safety information"],
  ["study_inventory", "Study inventory"],
  ["signal_log", "Signal log"],
];

export function SafetyQc({ reports, onChanged }: {
  reports: PvReportInstance[];
  onChanged?: () => void;
}) {
  const [reportId, setReportId] = useState(reports.length ? reports[0].id : "");
  const report = reports.find((r) => r.id === reportId) ?? null;

  if (!reports.length) {
    return (
      <PolishedEmpty
        icon={<ShieldCheck className="h-8 w-8 text-muted-foreground" />}
        title="No reporting interval yet"
        subtitle="Quality checks run against one report. Create one under Reporting intervals first."
      />
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-xs text-muted-foreground">Checks for</label>
        <select className={cn(SELECT_CLASS, "w-auto max-w-full")} value={reportId}
                onChange={(e) => setReportId(e.target.value)}>
          {reports.map((r) => (
            <option key={r.id} value={r.id}>
              {r.doc_type_name} · {r.period_start} → {r.period_end} · lock {r.data_lock_point}
            </option>
          ))}
        </select>
      </div>
      {report && <QcPanel report={report} onChanged={onChanged} key={report.id} />}
    </div>
  );
}

function QcPanel({ report, onChanged }: {
  report: PvReportInstance; onChanged?: () => void;
}) {
  const [qc, setQc] = useState<PvQcReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick((n) => n + 1), []);
  const changed = useCallback(() => { refresh(); onChanged?.(); }, [refresh, onChanged]);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    api.pvQc(report.id)
      .then((res) => { if (live) setQc(res); })
      .catch((e: any) => { if (live) setError(e?.message ?? String(e)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [report.id, tick]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Verdict qc={qc} loading={loading} />
        <Button size="sm" variant="outline" onClick={refresh} disabled={loading}>
          <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", loading && "animate-spin")} />
          Run checks again
        </Button>
      </div>
      {error && <ErrorBanner title="The checks could not run" message={error} onRetry={refresh} />}
      {loading && !qc && <TableSkeleton rows={6} />}
      {qc && (
        <div className="space-y-4">
          <FindingGroup title="Blockers" hint="Each one stops export."
                        findings={qc.blockers} tone="blocker"
                        reportId={report.id} onAccepted={refresh} />
          <FindingGroup title="Warnings" hint="Recorded with the export. They do not stop it."
                        findings={qc.warnings} tone="warning" reportId={report.id} />
          <FindingGroup title="Information" hint="Worth knowing before sign-off."
                        findings={qc.info} tone="info" reportId={report.id} />
        </div>
      )}
      <SignOffPanel report={report} qc={qc} onChanged={changed} />
      <ExportPanel report={report} qc={qc} onRefused={refresh} />
      <AuditPanel reportId={report.id} tick={tick} />
    </div>
  );
}

function Verdict({ qc, loading }: { qc: PvQcReport | null; loading: boolean }) {
  if (!qc) {
    return <p className="text-sm text-muted-foreground">{loading ? "Running checks…" : ""}</p>;
  }
  if (qc.exportable) {
    return (
      <div className="flex items-center gap-2 text-sm font-medium text-emerald-700 dark:text-emerald-400">
        <CheckCircle2 className="h-4 w-4" />
        No blockers. This report can be exported.
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2 text-sm font-medium text-destructive">
      <OctagonX className="h-4 w-4" />
      {qc.blockers.length} blocker{qc.blockers.length === 1 ? "" : "s"}. Export is refused until
      each is resolved.
    </div>
  );
}

const TONE = {
  blocker: { icon: OctagonX, text: "text-destructive", ring: "border-destructive/30" },
  warning: { icon: AlertTriangle, text: "text-amber-700 dark:text-amber-400",
             ring: "border-amber-500/30" },
  info: { icon: Info, text: "text-muted-foreground", ring: "border-border" },
} as const;

function FindingGroup({ title, hint, findings, tone, reportId, onAccepted }: {
  title: string; hint: string; findings: PvFinding[]; tone: keyof typeof TONE;
  reportId: string; onAccepted?: () => void;
}) {
  const style = TONE[tone];
  const Icon = style.icon;
  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className={cn("text-sm font-semibold", findings.length && style.text)}>
          {title} <span className="font-normal text-muted-foreground">({findings.length})</span>
        </h3>
        <span className="text-xs text-muted-foreground">{hint}</span>
      </div>
      {findings.length === 0 ? (
        <p className="text-xs text-muted-foreground">None.</p>
      ) : (
        <ul className="space-y-1.5">
          {findings.map((f, i) => (
            <li key={`${f.key}-${i}`}
                className={cn("rounded-md border px-3 py-2 text-sm", style.ring)}>
              <div className="flex items-start gap-2">
                <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", style.text)} />
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <code className="rounded bg-muted px-1.5 py-0.5 text-[11px]">{f.code}</code>
                    {f.section_code && (
                      <span className="rounded border px-1.5 py-0.5 text-[11px] text-muted-foreground">
                        § {f.section_code}
                      </span>
                    )}
                  </div>
                  <p className="break-words">{f.message}</p>
                  <Evidence detail={f.detail} />
                  {f.acceptable && onAccepted && (
                    <AcceptFinding reportId={reportId} finding={f} onAccepted={onAccepted} />
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function AcceptFinding({ reportId, finding, onAccepted }: {
  reportId: string; finding: PvFinding; onAccepted: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  async function accept() {
    setBusy(true);
    try {
      await api.pvAcceptFinding(reportId, finding.key, reason.trim());
      toast.success("Finding accepted", {
        description: "It stays on the list as a warning naming you and your reason.",
      });
      onAccepted();
    } catch (e: any) {
      toast.error("Could not accept the finding", { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button type="button" className="text-xs text-brand underline-offset-2 hover:underline"
              onClick={() => setOpen(true)}>
        Accept as correct (qualified person)…
      </button>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2 pt-1">
      <Input className="h-8 min-w-0 flex-1 text-xs" value={reason} autoFocus
             placeholder="Why the text is correct as written"
             onChange={(e) => setReason(e.target.value)} />
      <Button size="sm" disabled={busy || !reason.trim()} onClick={accept}>Accept</Button>
      <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
    </div>
  );
}

/** The ids and figures behind a finding, shown plainly: what the check saw. */
function Evidence({ detail }: { detail: Record<string, unknown> }) {
  const entries = Object.entries(detail ?? {}).filter(([, v]) => v !== null && v !== undefined);
  if (!entries.length) return null;
  return (
    <dl className="flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-muted-foreground">
      {entries.map(([k, v]) => (
        <div key={k} className="flex min-w-0 gap-1">
          <dt>{k.replace(/_/g, " ")}:</dt>
          <dd className="truncate font-mono" title={render(v)}>{render(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

function render(v: unknown): string {
  if (Array.isArray(v)) {
    const shown = v.slice(0, 5).map(String).join(", ");
    return v.length > 5 ? `${shown} … (+${v.length - 5})` : shown;
  }
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/* ------------------------------------------------------------------ sign-off */

function SignOffPanel({ report, qc, onChanged }: {
  report: PvReportInstance; qc: PvQcReport | null; onChanged: () => void;
}) {
  const [statement, setStatement] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const signed = Boolean(report.qppv_signoff_by);
  const others = qc?.blockers.filter((b) => b.code !== "NOT_SIGNED_OFF") ?? [];

  async function sign() {
    setBusy(true);
    try {
      await api.pvSignOff(report.id, statement.trim());
      toast.success("Report signed off", {
        description: "The figures it states are frozen. Any edit from here withdraws the signature.",
      });
      setStatement("");
      onChanged();
    } catch (e: any) {
      toast.error("Sign-off refused", { description: e?.message ?? String(e) });
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  async function withdraw() {
    setBusy(true);
    try {
      await api.pvWithdrawSignOff(report.id, reason.trim());
      toast.success("Sign-off withdrawn");
      setReason("");
      onChanged();
    } catch (e: any) {
      toast.error("Could not withdraw", { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-2 rounded-lg border p-4">
      <h3 className="flex items-center gap-2 text-sm font-semibold">
        <FileSignature className="h-4 w-4" /> Qualified-person sign-off
      </h3>
      {signed ? (
        <>
          <p className="text-sm">
            Signed off on {report.qppv_signoff_at?.slice(0, 10)}. Editing a section, withdrawing a
            section's approval or changing the report's terms takes this signature away.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Input className="h-8 min-w-0 flex-1 text-xs" value={reason}
                   placeholder="Reason for withdrawing the signature"
                   onChange={(e) => setReason(e.target.value)} />
            <Button size="sm" variant="outline" disabled={busy || !reason.trim()} onClick={withdraw}>
              <Undo2 className="mr-1.5 h-3.5 w-3.5" /> Withdraw
            </Button>
          </div>
        </>
      ) : (
        <>
          <p className="text-sm text-muted-foreground">
            {others.length
              ? `${others.length} other blocker${others.length === 1 ? "" : "s"} must be resolved before a qualified person can sign.`
              : "Nothing else blocks this report. Signing freezes the figures it states."}
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Input className="h-8 min-w-0 flex-1 text-xs" value={statement}
                   placeholder="Optional statement recorded with the signature"
                   onChange={(e) => setStatement(e.target.value)} />
            <Button size="sm" disabled={busy || !qc || others.length > 0} onClick={sign}>
              Sign off
            </Button>
          </div>
        </>
      )}
    </section>
  );
}

/* -------------------------------------------------------------------- export */

function ExportPanel({ report, qc, onRefused }: {
  report: PvReportInstance; qc: PvQcReport | null; onRefused: () => void;
}) {
  const [options, setOptions] = useState<PvExportOptions>({
    appendices: [], citations: "strip", draft_watermark: false,
    tracked_changes: false, region: null, pdf: false,
  });
  const [history, setHistory] = useState<PvExportRecord[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    api.pvExports(report.id)
      .then((res) => setHistory(res.items))
      .catch(() => setHistory([]));
  }, [report.id]);
  useEffect(load, [load]);

  function toggle(key: string) {
    setOptions((o) => ({
      ...o,
      appendices: o.appendices.includes(key)
        ? o.appendices.filter((a) => a !== key) : [...o.appendices, key],
    }));
  }

  async function run() {
    setBusy("export");
    try {
      const made = await api.pvExport(report.id, options);
      toast.success("Report exported", {
        description: `${made.files.length} file${made.files.length === 1 ? "" : "s"}; the identifier scan passed.`,
      });
      load();
    } catch (e: any) {
      toast.error("Export refused", { description: e?.message ?? String(e) });
      onRefused();
    } finally {
      setBusy(null);
    }
  }

  async function download(record: PvExportRecord, index: number) {
    setBusy(`${record.id}:${index}`);
    try {
      const url = await api.pvDownloadExport(record.id, index);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = record.files[index]?.filename ?? "report.docx";
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (e: any) {
      toast.error("Download failed", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="space-y-3 rounded-lg border p-4">
      <h3 className="flex items-center gap-2 text-sm font-semibold">
        <Download className="h-4 w-4" /> Export
      </h3>
      <p className="text-xs text-muted-foreground">
        This produces the report document only. Transmission to regulators — E2B, CIOMS or any
        gateway submission — belongs to the safety system of record.
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <fieldset className="space-y-1">
          <legend className="text-xs font-medium">Appendices</legend>
          {APPENDICES.map(([key, label]) => (
            <label key={key} className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={options.appendices.includes(key)}
                     onChange={() => toggle(key)} />
              {label}
            </label>
          ))}
        </fieldset>
        <div className="space-y-2 text-sm">
          <label className="flex items-center gap-2">
            Citations
            <select className={SELECT_CLASS} value={options.citations}
                    onChange={(e) => setOptions((o) => ({
                      ...o, citations: e.target.value as "strip" | "keep" }))}>
              <option value="strip">Remove [S#] markers</option>
              <option value="keep">Keep them (review copy)</option>
            </select>
          </label>
          {(report.regions ?? []).length > 0 && (
            <label className="flex items-center gap-2">
              Regional copy
              <select className={SELECT_CLASS} value={options.region ?? ""}
                      onChange={(e) => setOptions((o) => ({ ...o, region: e.target.value || null }))}>
                <option value="">None</option>
                {report.regions.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </label>
          )}
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={options.draft_watermark}
                   onChange={(e) => setOptions((o) => ({ ...o, draft_watermark: e.target.checked }))} />
            DRAFT watermark
          </label>
          <label className={cn("flex items-center gap-2", !report.baseline_report_id && "opacity-50")}
                 title={report.baseline_report_id ? undefined : "This report has no previous report to compare with."}>
            <input type="checkbox" checked={options.tracked_changes}
                   disabled={!report.baseline_report_id}
                   onChange={(e) => setOptions((o) => ({ ...o, tracked_changes: e.target.checked }))} />
            Tracked changes against the previous report
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={options.pdf}
                   onChange={(e) => setOptions((o) => ({ ...o, pdf: e.target.checked }))} />
            Also a PDF
          </label>
        </div>
      </div>
      <Button size="sm" disabled={!qc?.exportable || busy === "export"} onClick={run}>
        {busy === "export" ? "Exporting…" : "Export"}
      </Button>

      <div className="space-y-2 pt-2">
        <h4 className="text-xs font-medium text-muted-foreground">Export history</h4>
        {history === null ? (
          <TableSkeleton rows={2} />
        ) : history.length === 0 ? (
          <p className="text-xs text-muted-foreground">Nothing exported yet.</p>
        ) : (
          <ul className="space-y-2">
            {history.map((record) => (
              <li key={record.id} className="rounded-md border px-3 py-2 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-xs text-muted-foreground">
                    {record.created_at.slice(0, 16).replace("T", " ")}
                    {record.options.draft_watermark && " · DRAFT"}
                    {record.options.region && ` · ${record.options.region}`}
                    {record.options.warnings.length > 0
                      && ` · ${record.options.warnings.length} warning(s) recorded`}
                  </span>
                  <div className="flex flex-wrap gap-1.5">
                    {record.files.map((file) => (
                      <Button key={file.index} size="sm" variant="outline"
                              disabled={busy === `${record.id}:${file.index}`}
                              onClick={() => download(record, file.index)}>
                        <Download className="mr-1 h-3.5 w-3.5" /> {file.filename}
                      </Button>
                    ))}
                  </div>
                </div>
                {record.options.notes.map((note, i) => (
                  <p key={i} className="pt-1 text-xs text-muted-foreground">{note}</p>
                ))}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

/* --------------------------------------------------------------------- audit */

const PAGE = 25;

function AuditPanel({ reportId, tick }: { reportId: string; tick: number }) {
  const [scope, setScope] = useState<"report" | "product">("report");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<{ items: PvAuditEntry[]; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setError(null);
    api.pvAudit(reportId, { scope, limit: PAGE, offset })
      .then((res) => {
        if (!live) return;
        // A page emptied by a shrinking list steps back rather than showing nothing.
        if (!res.items.length && offset > 0) { setOffset(Math.max(0, offset - PAGE)); return; }
        setPage(res);
      })
      .catch((e: any) => { if (live) setError(e?.message ?? String(e)); });
    return () => { live = false; };
  }, [reportId, scope, offset, tick]);

  return (
    <section className="space-y-2 rounded-lg border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <History className="h-4 w-4" /> Audit trail
        </h3>
        <select className={SELECT_CLASS} value={scope}
                onChange={(e) => { setScope(e.target.value as "report" | "product"); setOffset(0); }}>
          <option value="report">This report, its sections and exports</option>
          <option value="product">Product data: cases, events, RSI</option>
        </select>
      </div>
      {error && <ErrorBanner title="The audit trail could not be read" message={error} />}
      {!page ? (
        <TableSkeleton rows={4} />
      ) : page.items.length === 0 ? (
        <p className="text-xs text-muted-foreground">No recorded events.</p>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-left text-muted-foreground">
                <tr>
                  <th className="py-1 pr-3 font-medium">When</th>
                  <th className="py-1 pr-3 font-medium">Who</th>
                  <th className="py-1 pr-3 font-medium">What</th>
                  <th className="py-1 font-medium">Detail</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((entry) => (
                  <tr key={entry.id} className="border-t align-top">
                    <td className="whitespace-nowrap py-1 pr-3">
                      {entry.created_at.slice(0, 16).replace("T", " ")}
                    </td>
                    <td className="py-1 pr-3">{entry.actor_name ?? entry.actor_id ?? "system"}</td>
                    <td className={cn("py-1 pr-3", entry.severity === "warning"
                      && "text-amber-700 dark:text-amber-400")}>{entry.event}</td>
                    <td className="break-words py-1 text-muted-foreground">{entry.target}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>Showing {offset + 1}–{offset + page.items.length} of {page.total}</span>
            <div className="flex gap-1.5">
              <Button size="sm" variant="outline" disabled={offset === 0}
                      onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</Button>
              <Button size="sm" variant="outline" disabled={offset + PAGE >= page.total}
                      onClick={() => setOffset(offset + PAGE)}>Next</Button>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
