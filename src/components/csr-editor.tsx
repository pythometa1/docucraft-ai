/**
 * The CSR generation workspace (screen S5): the section tree, the draft being
 * written, and the evidence it was written from.
 *
 * Three panes, because the question a medical writer asks of an AI-drafted
 * section is always "where did that number come from?" -- and the answer has
 * to be one click away, not a document search. Clicking a [S2, Table 14.1.1]
 * marker in the text scrolls to and highlights that exact chunk in the
 * Sources pane; clicking a source highlights its markers in the text.
 *
 * Nothing here writes prose on its own. Generation is per section, from that
 * section's retrieved sources; a regeneration is a NEW version, never an
 * overwrite, because what the model wrote and what the writer changed are
 * both part of the record.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  BookOpen, Check, FileWarning, Loader2, Sparkles, Table2, Wand2,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type { CsrDraft, CsrSection, CsrSource } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { StageSkeleton } from "@/components/skeletons";
import { DOC_TYPE_LABELS } from "@/components/csr-sources";
import { cn } from "@/lib/utils";
import { plainly } from "@/components/processing-banner";

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

/** Every [S#...] marker in a draft, with its position, so the text can be
 *  rendered with clickable citations without a markdown parser. */
const MARKER_RE = /\[S(\d+)(?:,\s*([^\]]+))?\]/g;
const DATA_NEEDED_RE = /\[DATA NEEDED:([^\]]*)\]/g;

export function CsrEditor({ csrProjectId, sections, onSectionsChanged }: {
  csrProjectId: string;
  sections: CsrSection[];
  onSectionsChanged: (sections: CsrSection[]) => void;
}) {
  const leaves = useMemo(() => sections.filter((s) => !s.is_container && s.enabled), [sections]);
  const [activeId, setActiveId] = useState<string | null>(leaves[0]?.id ?? null);
  const [draft, setDraft] = useState<CsrDraft | null>(null);
  const [sources, setSources] = useState<CsrSource[]>([]);
  const [versions, setVersions] = useState<number[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [tab, setTab] = useState<"sources" | "issues">("sources");
  const [instruction, setInstruction] = useState("");
  const [showInstruction, setShowInstruction] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [highlight, setHighlight] = useState<string | null>(null);
  const sourceRefs = useRef<Record<string, HTMLLIElement | null>>({});

  const active = sections.find((s) => s.id === activeId) ?? null;

  useEffect(() => {
    if (!activeId) return;
    let live = true;
    setLoading(true);
    setEditing(null);
    api.csrGetDraft(activeId)
      .then((res) => {
        if (!live) return;
        setDraft(res.draft);
        setSources(res.sources);
        setVersions(res.versions);
      })
      .catch((e) => { if (live) toast.error("Draft could not be loaded", { description: plainly(e?.message ?? String(e)) }); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [activeId]);

  async function loadVersion(version: number) {
    if (!activeId) return;
    setLoading(true);
    try {
      const res = await api.csrGetDraft(activeId, version);
      setDraft(res.draft);
      setSources(res.sources);
      setEditing(null);
    } finally {
      setLoading(false);
    }
  }

  async function generate() {
    if (!activeId) return;
    setBusy("generate");
    try {
      const res = await api.csrGenerateSection(activeId, instruction.trim() || undefined);
      setDraft(res);
      setVersions((prev) => [...prev, res.version]);
      setInstruction("");
      setShowInstruction(false);
      onSectionsChanged(sections.map((s) => (s.id === activeId ? res.section : s)));
      const reload = await api.csrGetDraft(activeId);
      setSources(reload.sources);
      if (res.data_needed.length) {
        setTab("issues");
        toast.warning(`Drafted with ${res.data_needed.length} gap${res.data_needed.length === 1 ? "" : "s"} — see Issues.`);
      } else {
        toast.success(`Section ${res.section.section_number} drafted (v${res.version}).`);
      }
    } catch (e: any) {
      toast.error("The section could not be drafted", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  async function save() {
    if (!activeId || editing === null) return;
    setBusy("save");
    try {
      const saved = await api.csrSaveDraft(activeId, editing);
      setDraft(saved);
      setVersions((prev) => [...prev, saved.version]);
      setEditing(null);
      toast.success(`Saved as version ${saved.version}.`);
      const res = await api.csrGetDraft(activeId);
      setSources(res.sources);
    } catch (e: any) {
      toast.error("Could not save", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  async function setStatus(status: string) {
    if (!activeId) return;
    setBusy("status");
    try {
      const updated = await api.csrSetSectionStatus(activeId, status);
      onSectionsChanged(sections.map((s) => (s.id === activeId ? updated : s)));
    } catch (e: any) {
      toast.error("Could not change status", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  const dataNeeded = useMemo(() => {
    if (!draft) return [];
    return Array.from(draft.content.matchAll(DATA_NEEDED_RE)).map((m) => m[1].trim());
  }, [draft]);

  const unresolved = useMemo(
    () => (draft?.citations ?? []).filter((c) => !c.chunk_id),
    [draft],
  );

  function focusSource(marker: string) {
    const source = sources.find((s) => s.marker === marker);
    if (!source) return;
    setTab("sources");
    setHighlight(source.chunk_id);
    sourceRefs.current[source.chunk_id]?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  /** The draft text with its citation markers turned into buttons. */
  function renderContent(text: string) {
    const parts: React.ReactNode[] = [];
    let cursor = 0;
    const combined = [
      ...Array.from(text.matchAll(MARKER_RE)).map((m) => ({ m, kind: "cite" as const })),
      ...Array.from(text.matchAll(DATA_NEEDED_RE)).map((m) => ({ m, kind: "gap" as const })),
    ].sort((a, b) => (a.m.index ?? 0) - (b.m.index ?? 0));

    combined.forEach(({ m, kind }, i) => {
      const start = m.index ?? 0;
      if (start > cursor) parts.push(text.slice(cursor, start));
      if (kind === "cite") {
        const marker = `S${m[1]}`;
        const known = sources.some((s) => s.marker === marker);
        parts.push(
          <button
            key={`c${i}`}
            onClick={() => focusSource(marker)}
            className={cn(
              "mx-0.5 rounded px-1 py-0.5 align-baseline font-mono text-[0.7rem] transition-colors",
              known
                ? "bg-brand/15 text-brand hover:bg-brand/25"
                : "bg-destructive/15 text-destructive",
            )}
            title={known ? "Show this source" : "This citation points at no known source"}
          >
            {m[0]}
          </button>,
        );
      } else {
        parts.push(
          <span key={`g${i}`}
                className="mx-0.5 rounded bg-warning/20 px-1 py-0.5 text-xs font-medium text-foreground"
                title="Information the sources did not contain">
            {m[0]}
          </span>,
        );
      }
      cursor = start + m[0].length;
    });
    if (cursor < text.length) parts.push(text.slice(cursor));
    return parts;
  }

  const depth = (n: string) => n.split(".").length - 1;

  return (
    <div className="grid gap-4 lg:grid-cols-[16rem_1fr_20rem]">
      {/* Left: the section tree */}
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
                  "flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-xs transition-colors",
                  section.is_container && "cursor-default font-semibold text-foreground",
                  !section.is_container && "hover:bg-accent",
                  activeId === section.id && "bg-accent",
                  !section.enabled && "opacity-40",
                )}
                style={{ paddingLeft: `${8 + depth(section.section_number) * 10}px` }}
              >
                <span className="min-w-0 truncate">
                  <span className="font-mono text-muted-foreground">{section.section_number}</span>{" "}
                  {section.title}
                </span>
                {!section.is_container && section.status !== "not_started" && (
                  <span className={cn("shrink-0 rounded-full border px-1.5 text-[0.6rem]",
                                      STATUS_TONE[section.status])}>
                    {STATUS_LABEL[section.status]}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      </div>

      {/* Centre: the draft */}
      <div className="space-y-3 rounded-xl border border-border bg-card p-4">
        {active === null ? (
          <p className="text-sm text-muted-foreground">Pick a section on the left.</p>
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-3">
              <div className="min-w-0">
                <h2 className="truncate text-base font-semibold text-foreground">
                  <span className="font-mono text-sm text-muted-foreground">{active.section_number}</span>{" "}
                  {active.title}
                </h2>
                {active.guidance_text && (
                  <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">{active.guidance_text}</p>
                )}
              </div>
              <div className="flex items-center gap-2">
                {versions.length > 1 && (
                  <select
                    className="h-8 rounded-md border border-input bg-transparent px-2 text-xs"
                    value={draft?.version ?? ""}
                    onChange={(e) => loadVersion(Number(e.target.value))}
                  >
                    {versions.map((v) => <option key={v} value={v}>v{v}</option>)}
                  </select>
                )}
                <select
                  className="h-8 rounded-md border border-input bg-transparent px-2 text-xs"
                  value={active.status === "not_started" ? "" : active.status}
                  onChange={(e) => setStatus(e.target.value)}
                  disabled={!draft || busy !== null}
                >
                  <option value="" disabled>Status</option>
                  <option value="draft">Draft</option>
                  <option value="in_review">In review</option>
                  <option value="approved">Approved</option>
                </select>
              </div>
            </div>

            {loading ? (
              <StageSkeleton lines={6} />
            ) : draft === null ? (
              <div className="space-y-3 py-6 text-center">
                <p className="text-sm text-muted-foreground">
                  Nothing drafted yet. This section will be written from your uploaded
                  sources — {DOC_TYPE_LABELS.protocol.toLowerCase()}, SAP, statistical
                  outputs — and every number it states will cite one.
                </p>
                <Button onClick={generate} disabled={busy !== null}>
                  {busy === "generate"
                    ? <><Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> Drafting…</>
                    : <><Wand2 className="mr-1.5 h-4 w-4" /> Generate this section</>}
                </Button>
              </div>
            ) : editing !== null ? (
              <div className="space-y-3">
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
                <div className="whitespace-pre-wrap text-sm leading-relaxed text-foreground">
                  {renderContent(draft.content)}
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3 text-xs text-muted-foreground">
                  <span>
                    {/* Which model drafted it is not the writer's concern, and not sent. */}
                    v{draft.version} · {draft.created_by === "ai" ? "AI draft" : "edited by hand"}
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
                    <Input
                      placeholder="Optional instruction: shorten, emphasise the subgroup analysis…"
                      value={instruction}
                      onChange={(e) => setInstruction(e.target.value)}
                    />
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

      {/* Right: sources and issues */}
      <div className="max-h-[70vh] overflow-hidden rounded-xl border border-border bg-card">
        <div className="flex border-b border-border">
          {([["sources", "Sources", BookOpen], ["issues", "Issues", FileWarning]] as const).map(
            ([key, label, Icon]) => (
              <button
                key={key}
                onClick={() => setTab(key)}
                className={cn(
                  "flex flex-1 items-center justify-center gap-1.5 border-b-2 px-3 py-2 text-xs font-medium transition-colors",
                  tab === key ? "border-brand text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
                )}
              >
                <Icon className="h-3.5 w-3.5" /> {label}
                {key === "issues" && (dataNeeded.length + unresolved.length) > 0 && (
                  <span className="rounded-full bg-warning/20 px-1.5 text-[0.65rem] text-foreground">
                    {dataNeeded.length + unresolved.length}
                  </span>
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
                  <li
                    key={source.chunk_id}
                    ref={(el) => { sourceRefs.current[source.chunk_id] = el; }}
                    className={cn(
                      "rounded-lg border p-2 text-xs transition-colors",
                      highlight === source.chunk_id
                        ? "border-brand bg-brand/10"
                        : "border-border bg-muted/20",
                    )}
                  >
                    <div className="mb-1 flex items-center gap-1.5 font-medium text-foreground">
                      <span className="rounded bg-brand/15 px-1 font-mono text-[0.65rem] text-brand">
                        {source.marker}
                      </span>
                      {source.is_table && <Table2 className="h-3 w-3 text-muted-foreground" />}
                      <span className="min-w-0 truncate">{source.filename ?? "source"}</span>
                    </div>
                    <div className="mb-1 text-[0.65rem] text-muted-foreground">
                      {DOC_TYPE_LABELS[source.doc_type] ?? source.doc_type}
                      {source.table_id ? ` · Table ${source.table_id}` : ""}
                      {source.page ? ` · p.${source.page}` : ""}
                    </div>
                    <p className="line-clamp-6 whitespace-pre-wrap text-muted-foreground">
                      {source.content}
                    </p>
                  </li>
                ))}
              </ul>
            )
          ) : (
            <div className="space-y-3 text-xs">
              {dataNeeded.length === 0 && unresolved.length === 0 ? (
                <p className="flex items-center gap-1.5 text-success">
                  <Check className="h-3.5 w-3.5" /> No gaps flagged in this section.
                </p>
              ) : (
                <>
                  {dataNeeded.length > 0 && (
                    <div>
                      <div className="mb-1 font-medium text-foreground">Missing information</div>
                      <ul className="space-y-1">
                        {dataNeeded.map((item, i) => (
                          <li key={i} className="rounded border border-warning/40 bg-warning/10 px-2 py-1 text-foreground">
                            {item}
                          </li>
                        ))}
                      </ul>
                      <p className="mt-1 text-muted-foreground">
                        Upload the source that carries this, then regenerate.
                      </p>
                    </div>
                  )}
                  {unresolved.length > 0 && (
                    <div>
                      <div className="mb-1 font-medium text-foreground">Unresolved citations</div>
                      <ul className="space-y-1">
                        {unresolved.map((c) => (
                          <li key={c.id} className="rounded border border-destructive/40 bg-destructive/10 px-2 py-1 text-foreground">
                            {c.marker} points at no source this section was drafted from.
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
