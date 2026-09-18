/**
 * S7: writing a periodic safety report against its predecessor.
 *
 * Three panes, and the layout encodes the module's rules rather than just
 * arranging them.
 *
 * - **Left**, the section tree, badged with what happened to each section this
 *   interval — carried forward, changed, new data — because a periodic report
 *   is written against the last one, and the first thing a writer needs to know
 *   about a section is whether anything moved.
 * - **Centre**, the draft. A `[TABLE: key]` line renders as a locked block drawn
 *   from the case store, with the words "edit in Case review": the model never
 *   typesets a table, and neither does the writer.
 * - **Right**, four tabs: the sources the draft cites, the confirmed figures it
 *   may quote, the previous report's text beside this one, and the issues that
 *   block approval — open [ASSESSMENT REQUIRED] judgments first, because those
 *   are the ones only a qualified person can close.
 */
import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, ArrowRightLeft, CheckCircle2, FileSearch, Gavel, Lock, Loader2,
  Save, Sparkles, Table2,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type {
  PvDelta, PvDraft, PvReportInstance, PvSection, PvTabulation,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ErrorBanner } from "@/components/error-banner";
import { PolishedEmpty, StageSkeleton } from "@/components/skeletons";
import { cn } from "@/lib/utils";

const SELECT_CLASS =
  "h-8 rounded-md border border-input bg-transparent px-2 text-xs";

const DELTA: Record<string, { label: string; tone: string }> = {
  carried_forward: { label: "Carried forward", tone: "bg-muted text-muted-foreground" },
  changed: { label: "Changed", tone: "bg-warning/15 text-warning" },
  new_data: { label: "New data", tone: "bg-brand/15 text-brand" },
  needs_rewrite: { label: "Needs rewrite", tone: "bg-destructive/15 text-destructive" },
  fresh: { label: "New", tone: "bg-muted text-muted-foreground" },
};

const STATUS_TONE: Record<string, string> = {
  not_started: "text-muted-foreground",
  generating: "text-brand",
  draft: "text-foreground",
  in_review: "text-warning",
  approved: "text-success",
};

const TABLE_LINE = /^[ \t]*\[TABLE:\s*([A-Za-z0-9_]+)\s*\][ \t]*$/;

export function SafetyEditor({ reports }: { reports: PvReportInstance[] }) {
  const [reportId, setReportId] = useState(reports.length ? reports[0].id : "");
  const [sections, setSections] = useState<PvSection[] | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [delta, setDelta] = useState<PvDelta | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    if (!reportId) return;
    let live = true;
    Promise.all([api.pvReportSections(reportId), api.pvDelta(reportId)])
      .then(([res, changes]) => {
        if (!live) return;
        setSections(res.items);
        setDelta(changes);
        setActiveId((current) => current
          ?? res.items.find((s) => !s.is_container)?.id ?? null);
      })
      .catch((e: any) => { if (live) { setError(e?.message ?? String(e)); setSections([]); } });
    return () => { live = false; };
  }, [reportId, reload]);

  if (!reports.length) {
    return (
      <PolishedEmpty
        icon={<FileSearch className="h-8 w-8 text-muted-foreground" />}
        title="No reporting interval yet"
        subtitle="Create one under Reporting intervals, and its section tree appears here to write."
      />
    );
  }
  if (sections === null) return <StageSkeleton lines={6} />;
  if (error) {
    return <ErrorBanner title="The report could not be opened"
                        message="Try again in a moment." detail={error} />;
  }

  const active = sections.find((s) => s.id === activeId) ?? null;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <select className={cn(SELECT_CLASS, "w-auto")} value={reportId}
                onChange={(e) => { setReportId(e.target.value); setActiveId(null); }}>
          {reports.map((r) => (
            <option key={r.id} value={r.id}>
              {r.doc_type_name} · {r.period_start} → {r.period_end}
            </option>
          ))}
        </select>
        {delta && <DeltaStrip delta={delta} />}
      </div>

      <div className="grid gap-4 lg:grid-cols-[16rem_1fr_20rem]">
        <nav className="max-h-[72vh] overflow-y-auto rounded-xl border border-border p-2">
          {sections.map((section) => (
            <button key={section.id}
                    disabled={section.is_container}
                    onClick={() => setActiveId(section.id)}
                    style={{ paddingLeft: `${0.5 + (section.level - 1) * 0.9}rem` }}
                    className={cn(
                      "flex w-full items-start gap-1.5 rounded-md py-1 pr-2 text-left text-xs",
                      section.is_container ? "cursor-default font-semibold text-foreground"
                        : activeId === section.id ? "bg-brand/10 text-foreground"
                        : "hover:bg-accent")}>
              <span className="w-8 shrink-0 font-mono text-muted-foreground">
                {section.section_code}
              </span>
              <span className="min-w-0 flex-1">
                <span className="line-clamp-2">{section.title}</span>
                {!section.is_container && (
                  <span className="mt-0.5 flex flex-wrap gap-1">
                    <span className={cn("text-[0.6rem]", STATUS_TONE[section.status])}>
                      {section.status.replace("_", " ")}
                    </span>
                    <span className={cn("rounded px-1 text-[0.6rem]",
                                        (DELTA[section.delta_status] ?? DELTA.fresh).tone)}>
                      {(DELTA[section.delta_status] ?? DELTA.fresh).label}
                    </span>
                    {section.table_key && (
                      <Table2 className="h-3 w-3 text-brand" aria-label="carries a table" />
                    )}
                  </span>
                )}
              </span>
            </button>
          ))}
        </nav>

        {active ? (
          <SectionPane key={active.id} reportId={reportId} section={active}
                       onChanged={() => setReload((n) => n + 1)} />
        ) : (
          <div className="lg:col-span-2">
            <PolishedEmpty icon={<FileSearch className="h-8 w-8 text-muted-foreground" />}
                           title="Choose a section" subtitle="Pick a section on the left to write it." />
          </div>
        )}
      </div>
    </div>
  );
}

/** "What changed this interval", computed from the store. The strip says so. */
function DeltaStrip({ delta }: { delta: PvDelta }) {
  const socs = Object.entries(delta.events_by_soc).slice(0, 3);
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground"
         title={delta.note}>
      <span className="inline-flex items-center gap-1">
        <ArrowRightLeft className="h-3 w-3" /> {delta.interval_cases} case(s) this interval
      </span>
      {socs.length > 0 && (
        <span>· top SOCs: {socs.map(([soc, n]) => `${soc} (${n})`).join(", ")}</span>
      )}
      {delta.signals_opened.length > 0 && <span>· {delta.signals_opened.length} signal(s) opened</span>}
      {delta.signals_closed.length > 0 && <span>· {delta.signals_closed.length} closed</span>}
      {delta.rsi_changes.length > 0 && <span>· RSI changed: {delta.rsi_changes.join(", ")}</span>}
      {delta.safety_actions.length > 0 && <span>· {delta.safety_actions.length} safety action(s)</span>}
    </div>
  );
}

type RightTab = "issues" | "sources" | "data" | "baseline";

function SectionPane({ reportId, section, onChanged }: {
  reportId: string; section: PvSection; onChanged: () => void;
}) {
  const [draft, setDraft] = useState<PvDraft | null | undefined>(undefined);
  const [versions, setVersions] = useState<number[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [instruction, setInstruction] = useState("");
  const [leakage, setLeakage] = useState<{ text: string; identifier_type: string }[]>([]);
  const [tab, setTab] = useState<RightTab>("issues");
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.pvGetDraft(section.id)
      .then((res) => { if (live) { setDraft(res.draft); setVersions(res.versions); } })
      .catch((e: any) => { if (live) { setDraft(null); toast.error("The draft could not be loaded",
                                                                   { description: e?.message ?? String(e) }); } });
    return () => { live = false; };
  }, [section.id]);

  async function generate() {
    setBusy("generate");
    try {
      const res = await api.pvGenerateSection(section.id, instruction.trim() || undefined);
      setDraft(res.draft);
      setVersions((v) => [...v, res.draft.version]);
      setEditing(null);
      setInstruction("");
      onChanged();
    } catch (e: any) {
      toast.error("The section could not be drafted", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function save() {
    if (editing === null) return;
    setBusy("save");
    try {
      const res = await api.pvSaveDraft(section.id, editing);
      setDraft(res.draft);
      setVersions((v) => [...v, res.draft.version]);
      setLeakage(res.leakage);
      setEditing(null);
      onChanged();
      if (res.leakage.length) {
        toast.warning(`${res.leakage.length} likely identifier(s) in this draft.`, {
          description: "Saved, and it cannot be approved until they are removed.",
        });
      }
    } catch (e: any) {
      toast.error("The draft could not be saved", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function setStatus(status: string) {
    setBusy("status");
    try {
      await api.pvSetSectionStatus(section.id, status);
      onChanged();
      toast.success(`${section.section_code} is ${status.replace("_", " ")}.`);
    } catch (e: any) {
      toast.error("The status could not be changed", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function openVersion(version: number) {
    if (editing !== null && editing !== draft?.content
        && !window.confirm("Switch version and discard your unsaved edit?")) return;
    try {
      const res = await api.pvGetDraft(section.id, version);
      setEditing(null);
      setDraft(res.draft);
    } catch (e: any) {
      toast.error(`Version ${version} could not be opened`,
                  { description: e?.message ?? String(e) });
    }
  }

  return (
    <>
      <div className="min-w-0 space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <h3 className="font-semibold">
              <span className="font-mono text-sm text-muted-foreground">{section.section_code}</span>{" "}
              {section.title}
            </h3>
            {section.guidance_text && (
              <p className="mt-0.5 line-clamp-3 text-xs text-muted-foreground">
                {section.guidance_text}
              </p>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            {versions.length > 1 && (
              <select className={SELECT_CLASS} value={draft?.version ?? ""}
                      onChange={(e) => openVersion(Number(e.target.value))}>
                {versions.map((v) => <option key={v} value={v}>v{v}</option>)}
              </select>
            )}
            <select className={SELECT_CLASS} value=""
                    disabled={!draft || busy !== null}
                    onChange={(e) => e.target.value && setStatus(e.target.value)}>
              <option value="" disabled>Status: {section.status.replace("_", " ")}</option>
              <option value="draft">Draft</option>
              <option value="in_review">In review</option>
              <option value="approved">Approve</option>
            </select>
          </div>
        </div>

        {draft === undefined ? <StageSkeleton lines={6} /> : editing !== null ? (
          <div className="space-y-2">
            <Textarea rows={20} value={editing} onChange={(e) => setEditing(e.target.value)}
                      className="font-mono text-xs" />
            <div className="flex justify-end gap-2">
              <Button variant="outline" size="sm" onClick={() => setEditing(null)}>Cancel</Button>
              <Button size="sm" onClick={save} disabled={busy !== null}>
                <Save className="mr-1 h-3.5 w-3.5" /> Save version
              </Button>
            </div>
          </div>
        ) : draft ? (
          <div className="space-y-2">
            <DraftView content={draft.content} reportId={reportId} />
            <div className="flex flex-wrap items-center justify-between gap-2 text-[0.65rem] text-muted-foreground">
              <span>
                v{draft.version} · {draft.origin.replace("_", " ")}
                {draft.model && ` · ${draft.model}`}
              </span>
              <Button variant="outline" size="sm" className="h-7"
                      onClick={() => setEditing(draft.content)}>
                Edit
              </Button>
            </div>
          </div>
        ) : (
          <PolishedEmpty icon={<Sparkles className="h-8 w-8 text-muted-foreground" />}
                         title="Not drafted yet"
                         subtitle="Generate a first draft from the masked sources and the confirmed figures, or write it yourself." />
        )}

        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-2">
          <Input className="h-8 flex-1 text-xs"
                 placeholder="Optional instruction for this revision…"
                 value={instruction} onChange={(e) => setInstruction(e.target.value)} />
          <Button size="sm" className="h-8" onClick={generate} disabled={busy !== null}>
            {busy === "generate"
              ? <><Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> Drafting…</>
              : <><Sparkles className="mr-1 h-3.5 w-3.5" /> {draft ? "Regenerate" : "Generate"}</>}
          </Button>
          {!draft && (
            <Button size="sm" variant="outline" className="h-8"
                    onClick={() => setEditing(`${section.section_code} ${section.title}\n\n`)}>
              Write it myself
            </Button>
          )}
        </div>
      </div>

      <aside className="min-w-0 space-y-2">
        <div className="flex gap-1 border-b border-border">
          {(["issues", "sources", "data", "baseline"] as RightTab[]).map((key) => (
            <button key={key} onClick={() => setTab(key)}
                    className={cn("border-b-2 px-2 py-1.5 text-xs font-medium capitalize",
                                  tab === key ? "border-brand text-foreground"
                                              : "border-transparent text-muted-foreground")}>
              {key}
            </button>
          ))}
        </div>
        {tab === "issues" && <IssuesTab draft={draft ?? null} section={section} leakage={leakage} />}
        {tab === "sources" && <SourcesTab draft={draft ?? null} />}
        {tab === "data" && <DataTab reportId={reportId} section={section} />}
        {tab === "baseline" && <BaselineTab section={section} version={draft?.version} />}
      </aside>
    </>
  );
}

/** The draft as it will read, with each table line replaced by a locked block
 *  drawn from the case store. */
function DraftView({ content, reportId }: { content: string; reportId: string }) {
  const parts = useMemo(() => {
    const out: { kind: "text" | "table"; value: string }[] = [];
    let buffer: string[] = [];
    for (const line of content.split("\n")) {
      const match = TABLE_LINE.exec(line);
      if (match) {
        if (buffer.length) out.push({ kind: "text", value: buffer.join("\n") });
        buffer = [];
        out.push({ kind: "table", value: match[1] });
      } else {
        buffer.push(line);
      }
    }
    if (buffer.length) out.push({ kind: "text", value: buffer.join("\n") });
    return out;
  }, [content]);

  return (
    <div className="space-y-3 rounded-xl border border-border p-4">
      {parts.map((part, index) => part.kind === "text" ? (
        <p key={index} className="whitespace-pre-wrap text-sm leading-relaxed">
          {highlight(part.value)}
        </p>
      ) : (
        <LockedTable key={index} reportId={reportId} tableKey={part.value} />
      ))}
    </div>
  );
}

/** Markers are shown as what they are: gaps and open judgments, not prose. */
function highlight(text: string) {
  const pieces = text.split(/(\[(?:DATA NEEDED|ASSESSMENT REQUIRED):[^\]]*\]|\[S\d+[^\]]*\])/g);
  return pieces.map((piece, i) => {
    if (piece.startsWith("[ASSESSMENT REQUIRED")) {
      return <mark key={i} className="rounded bg-destructive/15 px-0.5 text-destructive">{piece}</mark>;
    }
    if (piece.startsWith("[DATA NEEDED")) {
      return <mark key={i} className="rounded bg-warning/20 px-0.5 text-warning">{piece}</mark>;
    }
    if (/^\[S\d+/.test(piece)) {
      return <span key={i} className="text-[0.7rem] text-brand">{piece}</span>;
    }
    return <span key={i}>{piece}</span>;
  });
}

function LockedTable({ reportId, tableKey }: { reportId: string; tableKey: string }) {
  const [table, setTable] = useState<PvTabulation | null | undefined>(undefined);
  const [reason, setReason] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.pvTabulation(reportId, tableKey)
      .then((res) => { if (live) setTable(res); })
      .catch((e: any) => { if (live) { setTable(null); setReason(e?.message ?? String(e)); } });
    return () => { live = false; };
  }, [reportId, tableKey]);

  return (
    <div className="rounded-lg border border-brand/30 bg-brand/5">
      <div className="flex items-center gap-1.5 border-b border-brand/20 px-3 py-1.5 text-[0.65rem] text-brand">
        <Lock className="h-3 w-3" />
        Rendered from confirmed case data — edit in Case review · {tableKey}
      </div>
      {table === undefined ? (
        <div className="p-3"><StageSkeleton lines={2} /></div>
      ) : table === null ? (
        <p className="p-3 text-xs text-warning">
          This table cannot be built yet: {reason}
        </p>
      ) : (
        <div className="max-h-64 overflow-auto">
          <table className="w-full text-[0.65rem]">
            <thead>
              <tr className="text-left text-muted-foreground">
                {table.columns.map((c) => <th key={c} className="px-2 py-1 font-medium">{c}</th>)}
              </tr>
            </thead>
            <tbody>
              {table.rows.slice(0, 25).map((row, r) => (
                <tr key={r} className="border-t border-brand/10">
                  {row.map((v, c) => <td key={c} className="px-2 py-1">{v}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
          {table.rows.length > 25 && (
            <p className="px-3 py-1 text-[0.65rem] text-muted-foreground">
              …and {table.rows.length - 25} more rows in the report.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function IssuesTab({ draft, section, leakage }: {
  draft: PvDraft | null; section: PvSection;
  leakage: { text: string; identifier_type: string }[];
}) {
  if (!draft) return <p className="text-xs text-muted-foreground">Nothing drafted yet.</p>;
  const missingTable = section.table_key && !draft.table_markers.includes(section.table_key);
  const nothing = !draft.assessments_required.length && !draft.data_needed.length
    && !missingTable && !leakage.length;
  return (
    <div className="space-y-2 text-xs">
      {draft.assessments_required.map((item, i) => (
        <p key={`a${i}`} className="flex items-start gap-1.5 rounded-lg border border-destructive/30 bg-destructive/5 p-2">
          <Gavel className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
          <span><span className="font-medium">Qualified person:</span> {item || "(unspecified)"}</span>
        </p>
      ))}
      {missingTable && (
        <p className="flex items-start gap-1.5 rounded-lg border border-destructive/30 bg-destructive/5 p-2">
          <Table2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
          This section's table [TABLE: {section.table_key}] is not in the text.
        </p>
      )}
      {leakage.map((hit, i) => (
        <p key={`l${i}`} className="flex items-start gap-1.5 rounded-lg border border-destructive/30 bg-destructive/5 p-2">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
          Likely {hit.identifier_type.replace(/_/g, " ")}: “{hit.text}”
        </p>
      ))}
      {draft.data_needed.map((item, i) => (
        <p key={`d${i}`} className="flex items-start gap-1.5 rounded-lg border border-warning/30 bg-warning/5 p-2">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
          Data needed: {item || "(unspecified)"}
        </p>
      ))}
      {nothing && (
        <p className="flex items-center gap-1.5 text-success">
          <CheckCircle2 className="h-3.5 w-3.5" /> Nothing blocks approval in this text.
        </p>
      )}
    </div>
  );
}

function SourcesTab({ draft }: { draft: PvDraft | null }) {
  if (!draft?.source_map?.length) {
    return <p className="text-xs text-muted-foreground">No sources were retrieved for this version.</p>;
  }
  return (
    <ul className="space-y-1 text-xs">
      {draft.source_map.map((entry: any) => (
        <li key={entry.marker} className="rounded-md border border-border px-2 py-1">
          <span className="font-mono text-brand">[{entry.marker}]</span>{" "}
          {String(entry.doc_type ?? "source").replace(/_/g, " ")}
          {entry.page != null && ` · p.${entry.page}`}
          {entry.table_id && ` · table ${entry.table_id}`}
        </li>
      ))}
    </ul>
  );
}

function DataTab({ reportId, section }: { reportId: string; section: PvSection }) {
  const [table, setTable] = useState<PvTabulation | null>(null);
  useEffect(() => {
    if (!section.table_key) return;
    let live = true;
    api.pvTabulation(reportId, section.table_key)
      .then((res) => { if (live) setTable(res); })
      .catch(() => { /* shown as "no data yet" */ });
    return () => { live = false; };
  }, [reportId, section.table_key]);

  if (!section.table_key) {
    return <p className="text-xs text-muted-foreground">
      This section carries no computed table. It may quote the report's case counts, which
      the model is given already split into interval and cumulative.
    </p>;
  }
  if (!table) return <p className="text-xs text-muted-foreground">No data for this table yet.</p>;
  return (
    <div className="space-y-2 text-xs">
      <p className="text-muted-foreground">
        The figures a sentence in this section may quote, exactly as counted:
      </p>
      <ul className="space-y-0.5">
        {Object.entries(table.totals).map(([name, value]) => (
          <li key={name} className="flex justify-between gap-2">
            <span className="text-muted-foreground">{name.replace(/_/g, " ")}</span>
            <span className="font-mono">{value}</span>
          </li>
        ))}
      </ul>
      {table.missing.length > 0 && (
        <ul className="space-y-0.5 text-warning">
          {table.missing.map((gap, i) => <li key={i}>· {gap}</li>)}
        </ul>
      )}
    </div>
  );
}

function BaselineTab({ section, version }: { section: PvSection; version?: number }) {
  const [diff, setDiff] = useState<Awaited<ReturnType<typeof api.pvBaselineDiff>> | null>(null);
  useEffect(() => {
    let live = true;
    api.pvBaselineDiff(section.id)
      .then((res) => { if (live) setDiff(res); })
      .catch(() => { /* no baseline to show */ });
    return () => { live = false; };
  }, [section.id, version]);

  if (!diff) return <StageSkeleton lines={3} />;
  if (!diff.has_baseline) {
    return <p className="text-xs text-muted-foreground">
      No previous approved report holds this section.
    </p>;
  }
  if (!diff.changed) {
    return <p className="text-xs text-muted-foreground">
      Unchanged from the previous report. Check every statement against this interval's data
      before approving it again.
    </p>;
  }
  return (
    <pre className="max-h-[60vh] overflow-auto rounded-lg border border-border p-2 text-[0.65rem] leading-snug">
      {diff.diff.map((line, i) => (
        <div key={i} className={cn(
          line.startsWith("+") && !line.startsWith("+++") && "bg-success/10 text-success",
          line.startsWith("-") && !line.startsWith("---") && "bg-destructive/10 text-destructive")}>
          {line}
        </div>
      ))}
    </pre>
  );
}
