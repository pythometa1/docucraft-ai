/**
 * The CMC drafting workspace: sections on the left, the draft in the middle,
 * and the evidence behind it on the right.
 *
 * The difference from a narrative editor is the locked table block. Where a
 * section carries a specification, a batch analysis or a stability table, the
 * draft holds a `[TABLE: key]` marker and the editor renders the real table
 * beneath it, read from verified data. It is not editable here on purpose:
 * the numbers belong to the Data Review grid, and an editor that let somebody
 * retype one would create a document that disagrees with the store it claims
 * to come from.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  BookOpen, Check, FileWarning, Loader2, Lock, Sparkles, Table2, Wand2,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type { CmcDraft, CmcRenderedTable, CmcSection, CmcSource } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { StageSkeleton } from "@/components/skeletons";
import { cn } from "@/lib/utils";

const MARKER_RE = /\[S(\d+)(?:,\s*([^\]]+))?\]/g;
const DATA_NEEDED_RE = /\[DATA NEEDED:([^\]]*)\]/g;
const TABLE_RE = /^[ \t]*\[TABLE:\s*([A-Za-z0-9_]+)\s*\][ \t]*$/gm;

const STATUS_TONE: Record<string, string> = {
  not_started: "bg-muted text-muted-foreground border-border",
  generating: "bg-info/15 text-info border-info/30",
  draft: "bg-warning/15 text-warning border-warning/30",
  in_review: "bg-brand/15 text-brand border-brand/30",
  approved: "bg-success/15 text-success border-success/30",
};

const STATUS_LABEL: Record<string, string> = {
  not_started: "Not started", generating: "Generating", draft: "Draft",
  in_review: "In review", approved: "Approved",
};

/** A table rendered from the store, shown where its marker sits. */
function LockedTable({ table }: { table: CmcRenderedTable }) {
  return (
    <div className="my-3 overflow-hidden rounded-lg border border-brand/40 bg-brand/5">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-brand/30 px-3 py-1.5">
        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-foreground">
          <Lock className="h-3 w-3 text-brand" /> {table.title}
        </span>
        <span className="text-[0.65rem] text-muted-foreground">
          Rendered from verified data — edit in Data review
          {table.unverified > 0 && ` · ${table.unverified} unverified`}
        </span>
      </div>
      {/* One grid per group, because that is what the export writes. The flat
          `rows` union would show a stability summary as a single wide table
          the document does not contain. */}
      {(table.groups?.length ? table.groups
        : [{ title: "", columns: table.columns, rows: table.rows }]).map((group, groupIndex) => (
        <div key={groupIndex} className="overflow-x-auto">
          {group.title && (
            <div className="border-b border-brand/20 bg-brand/5 px-3 py-1 text-[0.65rem] font-medium text-foreground">
              {group.title}
            </div>
          )}
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-brand/20 bg-brand/5 text-left">
                {group.columns.map((column) => (
                  <th key={column} className="px-2 py-1 font-medium text-muted-foreground">{column}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {group.rows.map((row, index) => (
                <tr key={index} className="border-b border-border/40 last:border-0">
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex} className="px-2 py-1 font-mono">{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      {table.missing.length > 0 && (
        <p className="border-t border-brand/20 px-3 py-1.5 text-[0.65rem] text-warning">
          {table.missing.length} cell{table.missing.length === 1 ? "" : "s"} have no value yet.
        </p>
      )}
    </div>
  );
}

export function CmcEditor({ cmcProjectId, sections, onSectionsChanged }: {
  cmcProjectId: string;
  sections: CmcSection[];
  onSectionsChanged: (sections: CmcSection[]) => void;
}) {
  const leaves = useMemo(
    () => sections.filter((s) => !s.is_container && s.enabled),
    [sections],
  );
  const [activeId, setActiveId] = useState<string | null>(leaves[0]?.id ?? null);
  const [draft, setDraft] = useState<CmcDraft | null>(null);
  const [sources, setSources] = useState<CmcSource[]>([]);
  const [versions, setVersions] = useState<number[]>([]);
  const [tables, setTables] = useState<Record<string, CmcRenderedTable>>({});
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [tab, setTab] = useState<"sources" | "issues">("sources");
  const [instruction, setInstruction] = useState("");
  const [showInstruction, setShowInstruction] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [highlight, setHighlight] = useState<string | null>(null);
  const sourceRefs = useRef<Record<string, HTMLLIElement | null>>({});

  const active = sections.find((s) => s.id === activeId) ?? null;

  async function loadTables(content: string) {
    const keys = Array.from(content.matchAll(TABLE_RE)).map((m) => m[1]);
    const next: Record<string, CmcRenderedTable> = {};
    for (const key of new Set(keys)) {
      try {
        next[key] = await api.cmcPreviewTable(cmcProjectId, key);
      } catch { /* a table with no data shows as a gap, not a crash */ }
    }
    setTables(next);
  }

  useEffect(() => {
    if (!activeId) return;
    let live = true;
    setLoading(true);
    setEditing(null);
    setTables({});
    api.cmcGetDraft(activeId)
      .then(async (res) => {
        if (!live) return;
        setDraft(res.draft);
        setSources(res.sources);
        setVersions(res.versions);
        if (res.draft?.content) await loadTables(res.draft.content);
      })
      .catch((e) => { if (live) toast.error("Draft could not be loaded", { description: e?.message }); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [activeId]);

  async function generate() {
    if (!activeId) return;
    setBusy("generate");
    try {
      const res = await api.cmcGenerateSection(activeId, instruction.trim() || undefined);
      setDraft(res);
      setVersions((prev) => [...prev, res.version]);
      setInstruction("");
      setShowInstruction(false);
      onSectionsChanged(sections.map((s) => (s.id === activeId ? res.section : s)));
      await loadTables(res.content);
      const reload = await api.cmcGetDraft(activeId);
      setSources(reload.sources);
      if (res.data_needed.length) {
        setTab("issues");
        toast.warning(`Drafted with ${res.data_needed.length} gap${res.data_needed.length === 1 ? "" : "s"}.`);
      } else {
        toast.success(`${res.section.section_code} drafted (v${res.version}).`);
      }
    } catch (e: any) {
      toast.error("The section could not be drafted", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function save() {
    if (!activeId || editing === null) return;
    setBusy("save");
    try {
      const saved = await api.cmcSaveDraft(activeId, editing);
      setDraft(saved);
      setVersions((prev) => [...prev, saved.version]);
      setEditing(null);
      await loadTables(saved.content);
      toast.success(`Saved as version ${saved.version}.`);
    } catch (e: any) {
      toast.error("Could not save", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function setStatus(status: string) {
    if (!activeId) return;
    setBusy("status");
    try {
      const updated = await api.cmcSetSectionStatus(activeId, status);
      onSectionsChanged(sections.map((s) => (s.id === activeId ? updated : s)));
    } catch (e: any) {
      toast.error("Could not change status", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  const dataNeeded = useMemo(
    () => (draft ? Array.from(draft.content.matchAll(DATA_NEEDED_RE)).map((m) => m[1].trim()) : []),
    [draft],
  );

  function focusSource(marker: string) {
    const source = sources.find((s) => s.marker === marker);
    if (!source) return;
    setTab("sources");
    setHighlight(source.chunk_id);
    sourceRefs.current[source.chunk_id]?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  /** The draft, with citations clickable, gaps highlighted, and every
   *  [TABLE: key] replaced by the table it names. */
  function renderContent(text: string) {
    const out: React.ReactNode[] = [];
    const lines = text.split("\n");
    let buffer: string[] = [];

    const flush = (key: string) => {
      if (!buffer.length) return;
      const chunk = buffer.join("\n");
      buffer = [];
      const parts: React.ReactNode[] = [];
      let cursor = 0;
      const marks = [
        ...Array.from(chunk.matchAll(MARKER_RE)).map((m) => ({ m, kind: "cite" as const })),
        ...Array.from(chunk.matchAll(DATA_NEEDED_RE)).map((m) => ({ m, kind: "gap" as const })),
      ].sort((a, b) => (a.m.index ?? 0) - (b.m.index ?? 0));
      marks.forEach(({ m, kind }, i) => {
        const start = m.index ?? 0;
        if (start > cursor) parts.push(chunk.slice(cursor, start));
        if (kind === "cite") {
          const marker = `S${m[1]}`;
          const known = sources.some((s) => s.marker === marker);
          parts.push(
            <button key={`c${key}${i}`} onClick={() => focusSource(marker)}
                    className={cn("mx-0.5 rounded px-1 py-0.5 align-baseline font-mono text-[0.7rem]",
                                  known ? "bg-brand/15 text-brand hover:bg-brand/25"
                                        : "bg-destructive/15 text-destructive")}>
              {m[0]}
            </button>,
          );
        } else {
          parts.push(
            <span key={`g${key}${i}`}
                  className="mx-0.5 rounded bg-warning/20 px-1 py-0.5 text-xs font-medium">
              {m[0]}
            </span>,
          );
        }
        cursor = start + m[0].length;
      });
      if (cursor < chunk.length) parts.push(chunk.slice(cursor));
      out.push(<p key={`t${key}`} className="whitespace-pre-wrap">{parts}</p>);
    };

    lines.forEach((line, index) => {
      const match = /^[ \t]*\[TABLE:\s*([A-Za-z0-9_]+)\s*\][ \t]*$/.exec(line);
      if (match) {
        flush(`b${index}`);
        const table = tables[match[1]];
        out.push(table
          ? <LockedTable key={`tbl${index}`} table={table} />
          : <p key={`tbl${index}`}
               className="my-2 rounded border border-warning/40 bg-warning/10 px-2 py-1 text-xs">
              [TABLE: {match[1]}] — no data to render yet.
            </p>);
      } else {
        buffer.push(line);
      }
    });
    flush("end");
    return out;
  }

  const depth = (code: string) => code.split(".").length - 1;

  return (
    <div className="grid gap-4 lg:grid-cols-[16rem_1fr_20rem]">
      <div className="max-h-[70vh] overflow-y-auto rounded-xl border border-border bg-card">
        <div className="sticky top-0 border-b border-border bg-card px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Sections
        </div>
        <ul className="divide-y divide-border/40">
          {sections.map((section) => (
            <li key={section.id}>
              <button
                onClick={() => !section.is_container && setActiveId(section.id)}
                disabled={section.is_container}
                className={cn(
                  "flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-xs",
                  section.is_container && "cursor-default font-semibold text-foreground",
                  !section.is_container && "hover:bg-accent",
                  activeId === section.id && "bg-accent",
                  (!section.enabled || section.applicability !== "applicable") && "opacity-40",
                )}
                style={{ paddingLeft: `${8 + depth(section.section_code) * 10}px` }}
              >
                <span className="min-w-0 truncate">
                  <span className="font-mono text-muted-foreground">{section.section_code}</span>{" "}
                  {section.title}
                </span>
                <span className="flex shrink-0 items-center gap-1">
                  {section.table_key && <Table2 className="h-3 w-3 text-brand" />}
                  {!section.is_container && section.status !== "not_started" && (
                    <span className={cn("rounded-full border px-1.5 text-[0.6rem]",
                                        STATUS_TONE[section.status])}>
                      {STATUS_LABEL[section.status]}
                    </span>
                  )}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="space-y-3 rounded-xl border border-border bg-card p-4">
        {active === null ? (
          <p className="text-sm text-muted-foreground">Pick a section on the left.</p>
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-3">
              <div className="min-w-0">
                <h2 className="truncate text-base font-semibold text-foreground">
                  <span className="font-mono text-sm text-muted-foreground">{active.section_code}</span>{" "}
                  {active.title}
                </h2>
                {active.guidance_text && (
                  <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">{active.guidance_text}</p>
                )}
              </div>
              <div className="flex items-center gap-2">
                {versions.length > 1 && (
                  <select className="h-8 rounded-md border border-input bg-transparent px-2 text-xs"
                          value={draft?.version ?? ""}
                          onChange={async (e) => {
                            const res = await api.cmcGetDraft(active.id, Number(e.target.value));
                            setDraft(res.draft);
                            if (res.draft) await loadTables(res.draft.content);
                          }}>
                    {versions.map((v) => <option key={v} value={v}>v{v}</option>)}
                  </select>
                )}
                <select className="h-8 rounded-md border border-input bg-transparent px-2 text-xs"
                        value={active.status === "not_started" ? "" : active.status}
                        onChange={(e) => setStatus(e.target.value)}
                        disabled={!draft || busy !== null}>
                  <option value="" disabled>Status</option>
                  <option value="draft">Draft</option>
                  <option value="in_review">In review</option>
                  <option value="approved">Approved</option>
                </select>
              </div>
            </div>

            {active.applicability !== "applicable" ? (
              <div className="rounded-lg border border-border bg-muted/30 p-4 text-sm">
                <p className="font-medium text-foreground">
                  {active.applicability === "referenced_dmf"
                    ? "Covered by a referenced DMF"
                    : "Not applicable"}
                </p>
                <p className="mt-1 text-muted-foreground">
                  {active.applicability_justification || "No justification recorded."}
                </p>
                <p className="mt-2 text-xs text-muted-foreground">
                  The justification is this section's content in the export.
                </p>
              </div>
            ) : loading ? (
              <StageSkeleton lines={6} />
            ) : draft === null ? (
              <div className="space-y-3 py-6 text-center">
                <p className="text-sm text-muted-foreground">
                  Nothing drafted yet. This section is written from your indexed sources
                  {active.table_key && ", and its table is rendered from verified data rather than written"}.
                </p>
                <Button onClick={generate} disabled={busy !== null}>
                  {busy === "generate"
                    ? <><Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> Drafting…</>
                    : <><Wand2 className="mr-1.5 h-4 w-4" /> Draft this section</>}
                </Button>
              </div>
            ) : editing !== null ? (
              <div className="space-y-3">
                <p className="text-xs text-muted-foreground">
                  Table markers stay as <code className="font-mono">[TABLE: key]</code> — the table
                  itself is rendered from the store at export.
                </p>
                <Textarea rows={18} value={editing} onChange={(e) => setEditing(e.target.value)}
                          className="font-mono text-sm" />
                <div className="flex justify-end gap-2">
                  <Button variant="ghost" onClick={() => setEditing(null)}>Cancel</Button>
                  <Button onClick={save} disabled={busy !== null}>
                    {busy === "save" ? "Saving…" : "Save as new version"}
                  </Button>
                </div>
              </div>
            ) : (
              <div className="space-y-3">
                <div className="space-y-2 text-sm leading-relaxed text-foreground">
                  {renderContent(draft.content)}
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3 text-xs text-muted-foreground">
                  <span>
                    v{draft.version} · {draft.created_by === "ai"
                      ? `drafted by ${draft.model ?? "the model"}` : "edited by hand"}
                  </span>
                  <div className="flex gap-2">
                    <Button size="sm" variant="outline" onClick={() => setEditing(draft.content)}>Edit</Button>
                    <Button size="sm" variant="outline" onClick={() => setShowInstruction((v) => !v)}>
                      <Sparkles className="mr-1 h-3.5 w-3.5" /> Regenerate
                    </Button>
                  </div>
                </div>
                {showInstruction && (
                  <div className="flex gap-2">
                    <Input placeholder="Optional instruction: shorten, emphasise the process controls…"
                           value={instruction} onChange={(e) => setInstruction(e.target.value)} />
                    <Button onClick={generate} disabled={busy !== null}>
                      {busy === "generate" ? "Drafting…" : "Go"}
                    </Button>
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>

      <div className="max-h-[70vh] overflow-hidden rounded-xl border border-border bg-card">
        <div className="flex border-b border-border">
          {([["sources", "Sources", BookOpen], ["issues", "Issues", FileWarning]] as const).map(
            ([key, label, Icon]) => (
              <button key={key} onClick={() => setTab(key)}
                      className={cn(
                        "flex flex-1 items-center justify-center gap-1.5 border-b-2 px-3 py-2 text-xs font-medium",
                        tab === key ? "border-brand text-foreground"
                          : "border-transparent text-muted-foreground hover:text-foreground")}>
                <Icon className="h-3.5 w-3.5" /> {label}
                {key === "issues" && dataNeeded.length > 0 && (
                  <span className="rounded-full bg-warning/20 px-1.5 text-[0.65rem]">{dataNeeded.length}</span>
                )}
              </button>
            ))}
        </div>
        <div className="max-h-[calc(70vh-2.5rem)] overflow-y-auto p-3">
          {tab === "sources" ? (
            sources.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                The sources this section was drafted from appear here once it is generated.
              </p>
            ) : (
              <ul className="space-y-2">
                {sources.map((source) => (
                  <li key={source.chunk_id}
                      ref={(el) => { sourceRefs.current[source.chunk_id] = el; }}
                      className={cn("rounded-lg border p-2 text-xs",
                                    highlight === source.chunk_id
                                      ? "border-brand bg-brand/10" : "border-border bg-muted/20")}>
                    <div className="mb-1 flex items-center gap-1.5 font-medium text-foreground">
                      <span className="rounded bg-brand/15 px-1 font-mono text-[0.65rem] text-brand">
                        {source.marker}
                      </span>
                      {source.is_table && <Table2 className="h-3 w-3 text-muted-foreground" />}
                      <span className="min-w-0 truncate">{source.filename ?? "source"}</span>
                    </div>
                    <div className="mb-1 text-[0.65rem] text-muted-foreground">
                      {source.doc_type}{source.page ? ` · p.${source.page}` : ""}
                    </div>
                    <p className="line-clamp-6 whitespace-pre-wrap text-muted-foreground">
                      {source.content}
                    </p>
                  </li>
                ))}
              </ul>
            )
          ) : (
            <div className="space-y-2 text-xs">
              {dataNeeded.length === 0 ? (
                <p className="flex items-center gap-1.5 text-success">
                  <Check className="h-3.5 w-3.5" /> No gaps flagged in this section.
                </p>
              ) : (
                <>
                  <div className="font-medium text-foreground">Missing information</div>
                  <ul className="space-y-1">
                    {dataNeeded.map((item, i) => (
                      <li key={i} className="rounded border border-warning/40 bg-warning/10 px-2 py-1">
                        {item}
                      </li>
                    ))}
                  </ul>
                  <p className="text-muted-foreground">
                    Upload the source that carries this, then regenerate.
                  </p>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
