/**
 * S5: the case review grid, where the machine's proposals become a person's
 * determinations.
 *
 * The screen is built around one distinction. `Suggested` and `Confirmed` are
 * different columns, and the suggestion chip always carries its reasoning —
 * "Unlisted — no matching PT in CCDS 3.2" rather than a bare verdict. The
 * person confirming is accountable for the determination and cannot be
 * accountable for reasoning they cannot see.
 *
 * Nothing here writes a confirmed field without the qualified-person role, and
 * the header says plainly when the reader does not hold it: a row of disabled
 * buttons with no explanation is how somebody concludes the software is broken.
 */
import { useEffect, useState } from "react";
import {
  AlertTriangle, CheckCircle2, Copy, Lightbulb, ShieldQuestion, Sparkles, Tags,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type {
  PvCaseEvent, PvDuplicatePair, PvEventSummary, PvReportInstance,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorBanner } from "@/components/error-banner";
import { PolishedEmpty, TableSkeleton } from "@/components/skeletons";
import { SwapIn } from "@/components/motion";
import { cn } from "@/lib/utils";

const SELECT_CLASS =
  "h-8 rounded-md border border-input bg-transparent px-2 text-xs";

type Tab = "events" | "coding" | "duplicates";

const TABS: [Tab, string][] = [
  ["events", "Events"],
  ["coding", "Coding required"],
  ["duplicates", "Duplicates"],
];

export function SafetyReview({ productId, reports }: {
  productId: string;
  reports: PvReportInstance[];
}) {
  const [tab, setTab] = useState<Tab>("events");
  const [reportId, setReportId] = useState(reports.length ? reports[0].id : "");

  return (
    <div className="space-y-4">
      {reports.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-xs text-muted-foreground">
            Determinations are made against
          </label>
          <select className={cn(SELECT_CLASS, "w-auto")} value={reportId}
                  onChange={(e) => setReportId(e.target.value)}>
            {reports.map((r) => (
              <option key={r.id} value={r.id}>
                {r.doc_type_name} · {r.period_start} → {r.period_end}
              </option>
            ))}
          </select>
          <span className="text-xs text-muted-foreground">
            — an expectedness is a judgment about one reference safety information
            version, and this is the one it will be recorded against.
          </span>
        </div>
      )}

      <div className="flex gap-1 border-b border-border">
        {TABS.map(([key, label]) => (
          <button key={key} onClick={() => setTab(key)}
                  className={cn(
                    "border-b-2 px-3 py-2 text-sm font-medium transition-colors",
                    tab === key ? "border-brand text-foreground"
                                : "border-transparent text-muted-foreground hover:text-foreground")}>
            {label}
          </button>
        ))}
      </div>

      <SwapIn k={tab}>
        {tab === "events" && (
          <EventsTab productId={productId} reportId={reportId} />
        )}
        {tab === "coding" && (
          <EventsTab productId={productId} reportId={reportId} only="coding_required" />
        )}
        {tab === "duplicates" && <DuplicatesTab productId={productId} />}
      </SwapIn>
    </div>
  );
}

function EventsTab({ productId, reportId, only }: {
  productId: string; reportId: string; only?: string;
}) {
  const PAGE = 100;
  const [rows, setRows] = useState<PvCaseEvent[] | null>(null);
  const [summary, setSummary] = useState<PvEventSummary | null>(null);
  const [stale, setStale] = useState<{ event_id: string; meddra_pt: string }[]>([]);
  const [role, setRole] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState("");
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const timer = setTimeout(() => setQuery(filter.trim()), 300);
    return () => clearTimeout(timer);
  }, [filter]);
  useEffect(() => { setOffset(0); }, [query, only, reportId]);

  useEffect(() => {
    let live = true;
    api.pvCaseEvents(productId, {
      report_instance_id: reportId || undefined, only, q: query || undefined,
      limit: PAGE, offset,
    })
      .then((res) => {
        if (!live) return;
        setRows(res.items);
        setSummary(res.summary);
        setTotal(res.total);
        setRole(res.my_role);
        setStale(res.stale_expectedness ?? []);
      })
      .catch((e: any) => { if (live) { setError(e?.message ?? String(e)); setRows([]); } });
    return () => { live = false; };
  }, [productId, reportId, only, query, offset, reload]);

  const qualified = role === "qualified_person";

  async function suggest() {
    if (!reportId) {
      toast.error("Choose a reporting interval first — a suggestion is made "
                  + "against its pinned reference safety information.");
      return;
    }
    setBusy("suggest");
    try {
      const res = await api.pvSuggestExpectedness(reportId);
      setReload((n) => n + 1);
      toast.success(`${res.events} event(s) assessed.`, { description: res.note });
    } catch (e: any) {
      toast.error("Suggestions could not be computed",
                  { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function code() {
    setBusy("code");
    try {
      const res = await api.pvCodeEvents(productId);
      setReload((n) => n + 1);
      if (!res.dictionary_loaded) {
        toast.warning("No MedDRA dictionary is licensed in this deployment.", {
          description: "Nothing was coded automatically. An uncoded event is in "
                       + "no tabulation, so these need coding by hand.",
        });
      } else {
        toast.success(`${res.coded} event(s) coded against MedDRA ${res.meddra_version}.`);
      }
    } catch (e: any) {
      toast.error("Coding could not run", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function confirm(event: PvCaseEvent, expectedness: string) {
    setBusy(event.id);
    try {
      await api.pvConfirmEvent(event.id, { expectedness }, reportId || undefined);
      setReload((n) => n + 1);
    } catch (e: any) {
      toast.error("The determination could not be recorded",
                  { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function bulk(event: PvCaseEvent, expectedness: string) {
    if (!event.meddra_pt) return;
    if (!window.confirm(
      `Record "${expectedness}" for every event coded ${event.meddra_pt}? `
      + "Expectedness is a property of a term against one reference safety "
      + "information version, so this applies one determination consistently."
    )) return;
    setBusy(event.id);
    try {
      const res = await api.pvBulkConfirm(productId, {
        meddra_pt: event.meddra_pt, expectedness,
        report_instance_id: reportId || undefined,
      });
      setReload((n) => n + 1);
      toast.success(`${res.confirmed} event(s) confirmed.`);
    } catch (e: any) {
      toast.error("The determination could not be recorded",
                  { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  if (rows === null) return <TableSkeleton rows={6} cols={6} />;
  if (error) {
    return <ErrorBanner title="The events could not be loaded"
                        message="Try again in a moment." detail={error} />;
  }

  return (
    <div className="space-y-3">
      <div className={cn(
        "flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3",
        summary?.all_confirmed ? "border-success/40 bg-success/10"
                               : "border-warning/40 bg-warning/10")}>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {summary?.all_confirmed
            ? <CheckCircle2 className="h-4 w-4 text-success" />
            : <ShieldQuestion className="h-4 w-4 text-warning" />}
          <span className="font-medium text-foreground">
            {summary?.confirmed ?? 0} of {summary?.events ?? 0} events confirmed
          </span>
          {!!summary?.coding_required && (
            <span className="text-warning">
              · {summary.coding_required} uncoded
            </span>
          )}
          <span className="text-muted-foreground">
            {summary?.all_confirmed
              ? "— data-dependent sections can be generated."
              : "— unconfirmed determinations count nowhere."}
          </span>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={code} disabled={busy !== null}>
            <Tags className="mr-1 h-3.5 w-3.5" /> Code
          </Button>
          <Button variant="outline" size="sm" onClick={suggest} disabled={busy !== null}>
            <Sparkles className="mr-1 h-3.5 w-3.5" /> Suggest expectedness
          </Button>
        </div>
      </div>

      {!qualified && (
        <div className="flex items-start gap-2 rounded-lg border border-border bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          You hold the {(role ?? "no").replace("_", " ")} role on this product.
          Seriousness, expectedness and causality are confirmed by a qualified
          person; you can read the suggestions and their reasoning here.
        </div>
      )}

      {stale.length > 0 && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
          {stale.length} determination(s) were confirmed against a different
          reference safety information version than this report pins. They were not
          re-pointed — a judgment about one version is not a judgment about
          another — so each needs looking at again.
        </div>
      )}

      <Input value={filter} onChange={(e) => setFilter(e.target.value)}
             placeholder="Search reported term, preferred term or SOC…"
             className="h-8 w-80 text-xs" />

      {rows.length === 0 ? (
        <PolishedEmpty
          icon={<ShieldQuestion className="h-8 w-8 text-muted-foreground" />}
          title={only ? "Nothing needs coding" : "No events yet"}
          subtitle={only
            ? "Every event carries a MedDRA preferred term."
            : "Ingest an E2B export or a line listing, and its events appear here for review."}
        />
      ) : (
        <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Case</th>
                <th className="px-3 py-2 font-medium">Reported</th>
                <th className="px-3 py-2 font-medium">Coded</th>
                <th className="px-3 py-2 font-medium">Suggested</th>
                <th className="px-3 py-2 font-medium">Confirmed</th>
                <th className="px-3 py-2 text-right font-medium">Decide</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((event) => (
                <tr key={event.id} className="border-b border-border/60 last:border-0">
                  <td className="px-3 py-2 font-mono text-xs">
                    {event.worldwide_case_id ?? "—"}
                  </td>
                  <td className="max-w-[14rem] truncate px-3 py-2">
                    {event.verbatim_term ?? "—"}
                  </td>
                  <td className="px-3 py-2">
                    {event.meddra_pt ? (
                      <span>
                        {event.meddra_pt}
                        {event.meddra_soc && (
                          <span className="block text-[0.65rem] text-muted-foreground">
                            {event.meddra_soc}
                          </span>
                        )}
                      </span>
                    ) : (
                      <span className="rounded bg-warning/15 px-1.5 text-[0.65rem] text-warning">
                        needs coding
                      </span>
                    )}
                  </td>
                  <td className="max-w-[18rem] px-3 py-2">
                    <SuggestionChip event={event} />
                  </td>
                  <td className="px-3 py-2">
                    {event.expectedness === "not_assessed" ? (
                      <span className="text-xs text-muted-foreground">
                        not assessed
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-xs text-foreground">
                        <CheckCircle2 className="h-3 w-3 text-success" />
                        {event.expectedness}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right">
                    {qualified ? (
                      <div className="flex flex-wrap justify-end gap-1">
                        <Button size="sm" variant="outline" className="h-7 px-2 text-xs"
                                disabled={busy !== null}
                                onClick={() => confirm(event, "listed")}>
                          Listed
                        </Button>
                        <Button size="sm" variant="outline" className="h-7 px-2 text-xs"
                                disabled={busy !== null}
                                onClick={() => confirm(event, "unlisted")}>
                          Unlisted
                        </Button>
                        {event.meddra_pt && (
                          <Button size="sm" variant="outline"
                                  className="h-7 px-2 text-xs text-muted-foreground"
                                  disabled={busy !== null}
                                  title={`Apply one determination to every event coded ${event.meddra_pt}`}
                                  onClick={() => bulk(
                                    event,
                                    event.suggested?.expectedness?.value ?? "listed")}>
                            All “{event.meddra_pt}”
                          </Button>
                        )}
                      </div>
                    ) : (
                      <span className="text-xs text-muted-foreground">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {total > PAGE && (
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>
            Showing {Math.min(offset + 1, total)}–{Math.min(offset + PAGE, total)} of {total}
          </span>
          <div className="flex gap-1">
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

/** The chip §7 asks for: a proposal WITH its basis. "Unlisted" alone asks
 *  somebody to trust a verdict; "Unlisted — no matching PT in CCDS 3.2" asks
 *  them to check one, which is the only useful kind. */
function SuggestionChip({ event }: { event: PvCaseEvent }) {
  const suggestion = event.suggested?.expectedness;
  if (!suggestion) {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  return (
    <div className="flex items-start gap-1.5">
      <Lightbulb className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
      <div className="min-w-0 text-xs">
        {suggestion.value ? (
          <span className="font-medium text-foreground capitalize">
            {suggestion.value}
          </span>
        ) : (
          <span className="font-medium text-warning">Needs a person</span>
        )}
        <span className="block text-[0.65rem] text-muted-foreground">
          {suggestion.basis}
        </span>
      </div>
    </div>
  );
}

function DuplicatesTab({ productId }: { productId: string }) {
  const [pairs, setPairs] = useState<PvDuplicatePair[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let live = true;
    api.pvDuplicates(productId)
      .then((res) => { if (live) setPairs(res.items); })
      .catch((e: any) => { if (live) { setError(e?.message ?? String(e)); setPairs([]); } });
    return () => { live = false; };
  }, [productId, reload]);

  async function detect() {
    setBusy("detect");
    try {
      const res = await api.pvDetectDuplicates(productId);
      setReload((n) => n + 1);
      toast.success(`${res.candidates} candidate pair(s), ${res.new} new.`,
                    { description: res.note });
    } catch (e: any) {
      toast.error("Detection could not run", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function resolve(pair: PvDuplicatePair, action: string, keep?: string) {
    if (action === "merged" && !window.confirm(
      "Merge these two cases? The other case's events and narrative are removed "
      + "and every figure it contributed to changes. Re-importing will not undo it."
    )) return;
    setBusy(pair.id);
    try {
      await api.pvResolveDuplicate(pair.id, { action, keep_case_id: keep });
      setReload((n) => n + 1);
    } catch (e: any) {
      toast.error("The pair could not be resolved",
                  { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  if (pairs === null) return <TableSkeleton rows={4} cols={3} />;
  if (error) {
    return <ErrorBanner title="Candidates could not be loaded"
                        message="Try again in a moment." detail={error} />;
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-muted-foreground">
          Candidates only. A duplicate counted twice inflates every figure; two
          distinct cases merged loses one. Nothing is merged without a person.
        </span>
        <Button variant="outline" size="sm" onClick={detect} disabled={busy !== null}>
          <Copy className="mr-1 h-3.5 w-3.5" /> Detect
        </Button>
      </div>

      {pairs.length === 0 ? (
        <PolishedEmpty
          icon={<CheckCircle2 className="h-8 w-8 text-success" />}
          title="No candidate duplicates"
          subtitle="No pair of cases agrees on enough independent fields to be worth asking about."
        />
      ) : (
        <div className="space-y-3">
          {pairs.map((pair) => (
            <div key={pair.id} className="rounded-xl border border-border p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="grid flex-1 gap-3 sm:grid-cols-2">
                  {[pair.case, pair.other_case].map((side, index) => (
                    <div key={index} className="rounded-lg border border-border/60 p-2">
                      <div className="font-mono text-xs font-medium text-foreground">
                        {String(side.worldwide_case_id ?? side.id)}
                      </div>
                      <div className="mt-1 text-[0.65rem] text-muted-foreground">
                        {[side.country_of_occurrence, side.patient_sex,
                          side.patient_age ? `${side.patient_age}y` : null,
                          side.initial_receipt_date]
                          .filter(Boolean).join(" · ") || "—"}
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <ul className="mt-3 space-y-0.5">
                {pair.matched_on.map((note, index) => (
                  <li key={index} className="text-xs text-muted-foreground">· {note}</li>
                ))}
              </ul>

              <div className="mt-3 flex flex-wrap justify-end gap-1.5">
                <Button size="sm" variant="outline" className="h-7 px-2 text-xs"
                        disabled={busy !== null}
                        onClick={() => resolve(pair, "kept_both")}>
                  Keep both
                </Button>
                <Button size="sm" variant="outline" className="h-7 px-2 text-xs"
                        disabled={busy !== null}
                        onClick={() => resolve(pair, "linked")}>
                  Link
                </Button>
                <Button size="sm" variant="outline"
                        className="h-7 px-2 text-xs text-destructive hover:bg-destructive/10"
                        disabled={busy !== null}
                        onClick={() => resolve(pair, "merged",
                                               String(pair.case.id))}>
                  Merge into {String(pair.case.worldwide_case_id ?? "the first")}
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
