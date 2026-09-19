/**
 * S8: the signal log, and the screen that proposes candidates for it.
 *
 * A screen never creates a signal. It shows figures — each with the 2x2 table
 * behind it — and a person may raise a candidate from a row. A candidate is
 * not in any report until a reviewer validates it; refuting one keeps it on
 * the log with the reason, so the same term raised every quarter shows it was
 * looked at every quarter.
 *
 * The screening disclaimer sits beside the numbers, not in a footnote.
 */
import { useCallback, useEffect, useState } from "react";
import {
  Activity, AlertTriangle, ChevronDown, ChevronRight, Plus, Radar,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type {
  PvProduct, PvReportInstance, PvScreenResult, PvScreenRow, PvSignal,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ErrorBanner } from "@/components/error-banner";
import { PolishedEmpty, TableSkeleton } from "@/components/skeletons";
import { cn } from "@/lib/utils";
import { plainly } from "@/components/processing-banner";

const SELECT_CLASS =
  "h-8 rounded-md border border-input bg-transparent px-2 text-xs";

/** What each status may become, mirroring the server's lifecycle. */
const NEXT: Record<string, string[]> = {
  candidate: ["new", "refuted"],
  new: ["ongoing", "closed"],
  ongoing: ["closed"],
  closed: ["ongoing"],
  refuted: ["candidate"],
};

const STATUS_LABEL: Record<string, string> = {
  candidate: "Candidate", new: "Validated (new)", ongoing: "Ongoing",
  closed: "Closed", refuted: "Refuted",
};

const STATUS_TONE: Record<string, string> = {
  candidate: "bg-amber-500/10 text-amber-700 dark:text-amber-400",
  new: "bg-brand/10 text-brand",
  ongoing: "bg-brand/10 text-brand",
  closed: "bg-muted text-muted-foreground",
  refuted: "bg-muted text-muted-foreground",
};

export function SafetySignals({ product }: { product: PvProduct }) {
  const [signals, setSignals] = useState<PvSignal[] | null>(null);
  const [disclaimer, setDisclaimer] = useState("");
  const [filter, setFilter] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((n) => n + 1), []);

  useEffect(() => {
    let live = true;
    api.pvSignals(product.id, filter || undefined)
      .then((res) => { if (live) { setSignals(res.items); setDisclaimer(res.disclaimer); } })
      .catch((e: any) => { if (live) { setError(e?.message ?? String(e)); setSignals([]); } });
    return () => { live = false; };
  }, [product.id, filter, tick]);

  return (
    <div className="space-y-6">
      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <Activity className="h-4 w-4" /> Signal log
          </h3>
          <select className={SELECT_CLASS} value={filter}
                  onChange={(e) => setFilter(e.target.value)}>
            <option value="">All statuses</option>
            {Object.entries(STATUS_LABEL).map(([key, label]) => (
              <option key={key} value={key}>{label}</option>
            ))}
          </select>
        </div>
        <NewSignal productId={product.id} onCreated={reload} />
        {error && <ErrorBanner title="The signal log could not be read" message={plainly(error)} />}
        {signals === null ? (
          <TableSkeleton rows={4} />
        ) : signals.length === 0 ? (
          <PolishedEmpty
            icon={<Activity className="h-8 w-8 text-muted-foreground" />}
            title="No signals recorded"
            subtitle="Raise one from case review, literature or an authority request, or from a screening candidate below."
          />
        ) : (
          <ul className="space-y-2">
            {signals.map((signal) => (
              <SignalRow key={signal.id} signal={signal} reports={product.reports}
                         onChanged={reload} />
            ))}
          </ul>
        )}
      </section>
      <ScreenPanel product={product} disclaimer={disclaimer} onRaised={reload} />
    </div>
  );
}

function NewSignal({ productId, onCreated }: { productId: string; onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [terms, setTerms] = useState("");
  const [description, setDescription] = useState("");
  const [source, setSource] = useState("case_review");
  const [busy, setBusy] = useState(false);

  async function create() {
    setBusy(true);
    try {
      await api.pvCreateSignal(productId, {
        meddra_terms: terms.split(",").map((t) => t.trim()).filter(Boolean),
        description: description.trim() || null, detection_source: source,
      });
      toast.success("Candidate raised", {
        description: "A reviewer validates or refutes it before it enters a report.",
      });
      setTerms(""); setDescription(""); setOpen(false);
      onCreated();
    } catch (e: any) {
      toast.error("Could not raise the candidate", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        <Plus className="mr-1.5 h-3.5 w-3.5" /> Raise a candidate
      </Button>
    );
  }
  return (
    <div className="grid gap-2 rounded-lg border p-3 sm:grid-cols-[1fr_12rem]">
      <Input className="h-8 text-xs" value={terms} placeholder="MedDRA preferred terms, comma-separated"
             onChange={(e) => setTerms(e.target.value)} />
      <select className={SELECT_CLASS} value={source} onChange={(e) => setSource(e.target.value)}>
        <option value="case_review">Case review</option>
        <option value="literature">Literature</option>
        <option value="authority_request">Authority request</option>
        <option value="trial">Clinical trial</option>
        <option value="other">Other</option>
      </select>
      <Textarea className="text-xs sm:col-span-2" rows={2} value={description}
                placeholder="What was seen"
                onChange={(e) => setDescription(e.target.value)} />
      <div className="flex gap-2 sm:col-span-2">
        <Button size="sm" disabled={busy || (!terms.trim() && !description.trim())}
                onClick={create}>Raise candidate</Button>
        <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
      </div>
    </div>
  );
}

function SignalRow({ signal, reports, onChanged }: {
  signal: PvSignal; reports: PvReportInstance[]; onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState({
    evaluation_summary: signal.evaluation_summary ?? "",
    conclusion: signal.conclusion ?? "",
    action_taken: signal.action_taken ?? "",
  });
  const [busy, setBusy] = useState(false);

  async function save(extra: Partial<PvSignal> = {}) {
    setBusy(true);
    try {
      await api.pvUpdateSignal(signal.id, {
        evaluation_summary: draft.evaluation_summary || null,
        conclusion: draft.conclusion || null,
        action_taken: draft.action_taken || null,
        ...extra,
      });
      toast.success(extra.status ? `Signal is now ${STATUS_LABEL[extra.status] ?? extra.status}` : "Saved");
      onChanged();
    } catch (e: any) {
      toast.error("Not saved", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(false);
    }
  }

  const appeared = reports.filter((r) => signal.linked_report_instance_ids.includes(r.id));
  const basis = signal.detection_basis as Partial<PvScreenRow> | null;

  return (
    <li className="rounded-md border text-sm">
      <button type="button" onClick={() => setOpen((o) => !o)}
              className="flex w-full flex-wrap items-center gap-2 px-3 py-2 text-left">
        {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        <span className={cn("rounded px-1.5 py-0.5 text-[11px] font-medium",
                            STATUS_TONE[signal.status])}>
          {STATUS_LABEL[signal.status] ?? signal.status}
        </span>
        <span className="font-medium">
          {signal.signal_reference ? `${signal.signal_reference} · ` : ""}
          {signal.meddra_terms.join(", ") || signal.description}
        </span>
        <span className="ml-auto text-xs text-muted-foreground">
          {(signal.detection_source ?? "").replace(/_/g, " ")} · {signal.detection_date}
        </span>
      </button>
      {open && (
        <div className="space-y-3 border-t px-3 py-3">
          {signal.description && <p className="text-xs text-muted-foreground">{signal.description}</p>}
          {basis && basis.term && (
            <p className="rounded bg-muted px-2 py-1 text-xs">
              Raised from a screen: {basis.term} — a={basis.a}, PRR {basis.prr ?? "–"}
              {basis.prr_ci ? ` (${basis.prr_ci[0]}–${basis.prr_ci[1]})` : ""}, ROR {basis.ror ?? "–"}
              {basis.ror_ci ? ` (${basis.ror_ci[0]}–${basis.ror_ci[1]})` : ""}.
              Reporting frequency, not causality.
            </p>
          )}
          <label className="block space-y-1 text-xs">
            <span className="font-medium">Evaluation</span>
            <Textarea rows={3} value={draft.evaluation_summary}
                      onChange={(e) => setDraft((d) => ({ ...d, evaluation_summary: e.target.value }))} />
          </label>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="block space-y-1 text-xs">
              <span className="font-medium">Conclusion</span>
              <Textarea rows={2} value={draft.conclusion}
                        onChange={(e) => setDraft((d) => ({ ...d, conclusion: e.target.value }))} />
            </label>
            <label className="block space-y-1 text-xs">
              <span className="font-medium">Action taken</span>
              <Textarea rows={2} value={draft.action_taken} placeholder="or 'None'"
                        onChange={(e) => setDraft((d) => ({ ...d, action_taken: e.target.value }))} />
            </label>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" disabled={busy} onClick={() => save()}>Save</Button>
            {(NEXT[signal.status] ?? []).map((next) => (
              <Button key={next} size="sm" disabled={busy}
                      variant={next === "refuted" ? "outline" : "default"}
                      onClick={() => save({ status: next })}>
                {next === "new" ? "Validate" : next === "refuted" ? "Refute"
                  : next === "closed" ? "Close" : next === "candidate" ? "Raise again"
                  : `Mark ${STATUS_LABEL[next]?.toLowerCase() ?? next}`}
              </Button>
            ))}
            <span className="text-xs text-muted-foreground">
              Validating, refuting and closing are a reviewer's. Closing needs a conclusion and the action taken.
            </span>
          </div>
          {appeared.length > 0 && (
            <p className="text-xs text-muted-foreground">
              Appeared in: {appeared.map((r) => `${r.doc_type_name} ${r.period_start} → ${r.period_end}`).join("; ")}
            </p>
          )}
        </div>
      )}
    </li>
  );
}

/* ------------------------------------------------------------------ the screen */

function fmt(value: number | null, ci: [number, number] | null) {
  if (value === null) return "–";
  return ci ? `${value} (${ci[0]}–${ci[1]})` : String(value);
}

function ScreenPanel({ product, disclaimer, onRaised }: {
  product: PvProduct; disclaimer: string; onRaised: () => void;
}) {
  const [reportId, setReportId] = useState(product.reports[0]?.id ?? "");
  const [settings, setSettings] = useState<{
    window: "interval" | "cumulative"; level: "pt" | "soc"; min_cases: number;
  }>({ window: "cumulative", level: "pt", min_cases: 1 });
  const [result, setResult] = useState<PvScreenResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function run() {
    setBusy("run");
    setError(null);
    try {
      setResult(await api.pvDisproportionality(reportId, settings));
    } catch (e: any) {
      setResult(null);
      setError(e?.message ?? String(e));
    } finally {
      setBusy(null);
    }
  }

  async function raise(row: PvScreenRow) {
    if (!result) return;
    setBusy(row.term);
    try {
      await api.pvCreateSignal(product.id, {
        meddra_terms: [row.term], detection_source: "disproportionality",
        description: `Screening candidate: ${row.term} (${result.window} window, ${result.background_basis}).`,
        detection_basis: { ...row, window: result.window, level: result.level,
                           period: result.period, background: result.background_basis,
                           disclaimer: result.disclaimer },
      });
      toast.success("Candidate raised", {
        description: "It enters no report until a reviewer validates it.",
      });
      onRaised();
    } catch (e: any) {
      toast.error("Could not raise the candidate", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  if (!product.reports.length) return null;

  return (
    <section className="space-y-3 rounded-lg border p-4">
      <h3 className="flex items-center gap-2 text-sm font-semibold">
        <Radar className="h-4 w-4" /> Disproportionality screen
      </h3>
      <p className="flex items-start gap-2 rounded-md bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        {plainly(disclaimer) || "Screening statistic — indicates reporting frequency, not causality; not evidence of a causal association."}
      </p>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <select className={cn(SELECT_CLASS, "max-w-full")} value={reportId}
                onChange={(e) => setReportId(e.target.value)}>
          {product.reports.map((r) => (
            <option key={r.id} value={r.id}>
              {r.doc_type_name} · {r.period_start} → {r.period_end} · lock {r.data_lock_point}
            </option>
          ))}
        </select>
        <select className={SELECT_CLASS} value={settings.window}
                onChange={(e) => setSettings((s) => ({ ...s, window: e.target.value as "interval" | "cumulative" }))}>
          <option value="cumulative">Cumulative to the lock</option>
          <option value="interval">This interval</option>
        </select>
        <select className={SELECT_CLASS} value={settings.level}
                onChange={(e) => setSettings((s) => ({ ...s, level: e.target.value as "pt" | "soc" }))}>
          <option value="pt">Preferred term</option>
          <option value="soc">System organ class</option>
        </select>
        <label className="flex items-center gap-1">
          Minimum cases
          <Input type="number" min={1} className="h-8 w-16 text-xs" value={settings.min_cases}
                 onChange={(e) => setSettings((s) => ({ ...s, min_cases: Math.max(1, Number(e.target.value) || 1) }))} />
        </label>
        <Button size="sm" disabled={!reportId || busy === "run"} onClick={run}>
          {busy === "run" ? "Screening…" : "Run screen"}
        </Button>
      </div>
      {error && <ErrorBanner title="The screen could not run" message={plainly(error)} />}
      {result && (
        <div className="space-y-2">
          <p className="text-xs text-muted-foreground">
            {result.product_cases} case(s) with this product against {result.background_cases} background
            case(s): {result.background_basis}. Thresholds — Evans: {result.thresholds.evans}; ROR: {result.thresholds.ror}.
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-left text-muted-foreground">
                <tr>
                  <th className="py-1 pr-3 font-medium">Term</th>
                  <th className="py-1 pr-3 font-medium" title="a / b / c / d">2×2</th>
                  <th className="py-1 pr-3 font-medium">PRR (95% CI)</th>
                  <th className="py-1 pr-3 font-medium">ROR (95% CI)</th>
                  <th className="py-1 pr-3 font-medium">χ²</th>
                  <th className="py-1 font-medium" />
                </tr>
              </thead>
              <tbody>
                {result.rows.map((row) => (
                  <tr key={row.term} className={cn("border-t align-top",
                                                   row.screening_flag && "bg-amber-500/5")}>
                    <td className="py-1 pr-3 font-medium">
                      {row.term}
                      {row.screening_flag && (
                        <span className="ml-1.5 rounded bg-amber-500/15 px-1 text-[10px] text-amber-800 dark:text-amber-300">
                          meets a screening threshold
                        </span>
                      )}
                      {row.notes.map((note, i) => (
                        <div key={i} className="font-normal text-muted-foreground">{plainly(note)}</div>
                      ))}
                    </td>
                    <td className="whitespace-nowrap py-1 pr-3 font-mono">
                      {row.a} / {row.b} / {row.c} / {row.d}
                    </td>
                    <td className="whitespace-nowrap py-1 pr-3">{fmt(row.prr, row.prr_ci)}</td>
                    <td className="whitespace-nowrap py-1 pr-3">{fmt(row.ror, row.ror_ci)}</td>
                    <td className="py-1 pr-3">{row.chi2 ?? "–"}</td>
                    <td className="py-1">
                      <Button size="sm" variant="outline" disabled={busy === row.term}
                              onClick={() => raise(row)}>
                        Raise candidate
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
