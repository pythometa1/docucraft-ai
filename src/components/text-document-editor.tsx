/* The narrow editor: change the words, never the document.
 *
 * A letter produced by filling a Word template cannot be rebuilt from HTML
 * without losing everything HTML has no word for -- section breaks, headers,
 * numbering, cell borders. That is why `saveDocumentVersion` refuses these
 * documents, and why this editor does not render HTML at all. It renders the
 * document's runs, addressed by (paragraph_index, span_index) -- the same
 * coordinates the compiler and fill engine use -- and a save writes text back
 * into those exact runs. Every other byte of the .docx is untouched.
 *
 * So the affordances here are deliberately fewer than a word processor's. There
 * is no bold button, no way to add a paragraph, no table editing. A reviewer who
 * needs to restructure a letter downloads it and opens Word; a reviewer who
 * needs to fix a sentence does it here, with the layout guaranteed intact.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Check, Loader2, RotateCcw, Sparkles, Undo2, X } from "lucide-react";

import { api } from "@/lib/api";

type Span = {
  paragraph_index: number;
  span_index: number;
  text: string;
  in_table: boolean;
  role: string;
};
type Para = { paragraph_index: number; in_table: boolean; text: string; spans: Span[] };
type Key = string;

const keyOf = (p: number, s: number): Key => `${p}:${s}`;

export function TextDocumentEditor({
  versionId,
  filename,
  onSaved,
}: {
  versionId: string;
  filename: string;
  onSaved?: (newVersionId: string, versionNo: number) => void;
}) {
  const [paragraphs, setParagraphs] = useState<Para[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  /** Edited text by span, holding only what the reviewer actually changed. */
  const [drafts, setDrafts] = useState<Record<Key, string>>({});
  const [selected, setSelected] = useState<Span | null>(null);
  const [instruction, setInstruction] = useState("");
  const [suggestion, setSuggestion] = useState<{ replacement: string; note: string; model: string } | null>(null);
  const [asking, setAsking] = useState(false);
  const [saving, setSaving] = useState(false);
  const areaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .documentText(versionId)
      .then((d) => !cancelled && setParagraphs(d.paragraphs))
      .catch((e: any) => !cancelled && setLoadError(e?.message ?? String(e)));
    return () => {
      cancelled = true;
    };
  }, [versionId]);

  const textOf = (s: Span) => drafts[keyOf(s.paragraph_index, s.span_index)] ?? s.text;

  /** Only spans whose text actually differs. An edit that changes nothing is
   *  refused by the server, so sending it would surface as an error the
   *  reviewer did not cause. */
  const pending = useMemo(() => {
    if (!paragraphs) return [];
    const out: { paragraph_index: number; span_index: number; text: string; before: string }[] = [];
    for (const p of paragraphs) {
      for (const s of p.spans) {
        const next = drafts[keyOf(s.paragraph_index, s.span_index)];
        if (next !== undefined && next !== s.text) {
          out.push({ paragraph_index: s.paragraph_index, span_index: s.span_index, text: next, before: s.text });
        }
      }
    }
    return out;
  }, [paragraphs, drafts]);

  function select(s: Span) {
    setSelected(s);
    setSuggestion(null);
    setInstruction("");
    requestAnimationFrame(() => areaRef.current?.focus());
  }

  function setDraft(s: Span, value: string) {
    setDrafts((d) => ({ ...d, [keyOf(s.paragraph_index, s.span_index)]: value }));
  }

  function revert(s: Span) {
    setDrafts((d) => {
      const next = { ...d };
      delete next[keyOf(s.paragraph_index, s.span_index)];
      return next;
    });
  }

  async function ask() {
    if (!selected || !instruction.trim()) return;
    setAsking(true);
    setSuggestion(null);
    try {
      const r = await api.suggestEdit(versionId, {
        selection: textOf(selected),
        instruction: instruction.trim(),
        paragraph_index: selected.paragraph_index,
      });
      setSuggestion(r);
    } catch (e: any) {
      toast.error("Could not get a suggestion", { description: e?.message ?? String(e) });
    } finally {
      setAsking(false);
    }
  }

  async function save() {
    if (!pending.length) return;
    setSaving(true);
    try {
      const res = await api.applyTextEdits(
        versionId,
        pending.map(({ paragraph_index, span_index, text }) => ({ paragraph_index, span_index, text })),
      );
      toast.success(`Saved as version ${res.version_no}`, { description: res.change_summary });
      setDrafts({});
      setSelected(null);
      onSaved?.(res.version_id, res.version_no);
    } catch (e: any) {
      toast.error("Could not save", { description: e?.message ?? String(e) });
    } finally {
      setSaving(false);
    }
  }

  if (loadError) {
    return (
      <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm">
        <p className="font-medium text-destructive">This document could not be opened for editing.</p>
        <p className="mt-1 text-muted-foreground">{loadError}</p>
      </div>
    );
  }
  if (!paragraphs) {
    return (
      <div className="flex items-center gap-2 p-8 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Reading the document…
      </div>
    );
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
      {/* ---------------------------------------------------------- document */}
      <div className="min-w-0">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold">{filename}</h2>
            <p className="text-xs text-muted-foreground">
              Click any line to edit its words. Formatting, tables and headers are preserved —
              they cannot be changed here.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {pending.length > 0 && (
              <span className="rounded-full bg-amber-500/15 px-2.5 py-1 text-xs font-medium text-amber-600 dark:text-amber-400">
                {pending.length} unsaved change{pending.length === 1 ? "" : "s"}
              </span>
            )}
            <button
              onClick={save}
              disabled={!pending.length || saving}
              className="inline-flex items-center gap-2 rounded-md bg-brand px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40"
            >
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
              Submit changes
            </button>
          </div>
        </div>

        <div className="max-h-[70vh] overflow-y-auto rounded-lg border border-border bg-background p-6 shadow-sm">
          <div className="mx-auto max-w-[70ch] space-y-1">
            {paragraphs.map((p) => {
              if (!p.spans.length) return <div key={p.paragraph_index} className="h-3" aria-hidden />;
              return (
                <p key={p.paragraph_index} className={p.in_table ? "text-sm" : ""}>
                  {p.spans.map((s) => {
                    const changed = drafts[keyOf(s.paragraph_index, s.span_index)] !== undefined
                      && drafts[keyOf(s.paragraph_index, s.span_index)] !== s.text;
                    const active = selected
                      && selected.paragraph_index === s.paragraph_index
                      && selected.span_index === s.span_index;
                    return (
                      <button
                        key={s.span_index}
                        onClick={() => select(s)}
                        className={[
                          "rounded px-0.5 text-left align-baseline transition-colors",
                          active ? "bg-brand/20 ring-1 ring-brand" : "hover:bg-accent",
                          changed ? "bg-amber-500/15 underline decoration-amber-500 decoration-2" : "",
                        ].join(" ")}
                        title={`Paragraph ${s.paragraph_index}, run ${s.span_index}`}
                      >
                        {textOf(s) || <span className="text-muted-foreground">(empty)</span>}
                      </button>
                    );
                  })}
                </p>
              );
            })}
          </div>
        </div>
      </div>

      {/* ------------------------------------------------------------- panel */}
      <aside className="space-y-3">
        <div className="rounded-lg border border-border bg-card p-4">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <Sparkles className="h-4 w-4 text-brand" /> Edit with AI
          </h3>

          {!selected ? (
            <p className="mt-3 text-sm text-muted-foreground">
              Select a line in the document to rewrite it, on its own or with the assistant.
            </p>
          ) : (
            <div className="mt-3 space-y-3">
              <div>
                <label className="text-xs font-medium text-muted-foreground">
                  Selected — paragraph {selected.paragraph_index}
                </label>
                <textarea
                  ref={areaRef}
                  value={textOf(selected)}
                  onChange={(e) => setDraft(selected, e.target.value)}
                  rows={5}
                  className="mt-1 w-full resize-y rounded-md border border-border bg-background p-2 text-sm"
                />
                <p className="mt-1 text-[11px] text-muted-foreground">
                  Line breaks and tabs cannot be saved into a run — Word stores those as elements,
                  so they would vanish. Edit one line at a time.
                </p>
                {drafts[keyOf(selected.paragraph_index, selected.span_index)] !== undefined && (
                  <button
                    onClick={() => revert(selected)}
                    className="mt-1 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                  >
                    <Undo2 className="h-3 w-3" /> Revert this line
                  </button>
                )}
              </div>

              <div>
                <label className="text-xs font-medium text-muted-foreground">What should change?</label>
                <div className="mt-1 flex gap-2">
                  <input
                    value={instruction}
                    onChange={(e) => setInstruction(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && ask()}
                    placeholder="Make it more formal…"
                    className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1.5 text-sm"
                  />
                  <button
                    onClick={ask}
                    disabled={asking || !instruction.trim()}
                    className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-sm disabled:opacity-40"
                  >
                    {asking ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                    Ask
                  </button>
                </div>
              </div>

              {suggestion && (
                <div className="rounded-md border border-brand/40 bg-brand/5 p-3">
                  <p className="text-xs font-medium text-brand">Suggested</p>
                  <p className="mt-1 whitespace-pre-wrap text-sm">{suggestion.replacement}</p>
                  {suggestion.note && (
                    <p className="mt-2 border-t border-brand/20 pt-2 text-xs text-muted-foreground">
                      {suggestion.note}
                    </p>
                  )}
                  <div className="mt-3 flex items-center gap-2">
                    <button
                      onClick={() => {
                        setDraft(selected, suggestion.replacement);
                        setSuggestion(null);
                      }}
                      className="inline-flex items-center gap-1.5 rounded-md bg-brand px-2.5 py-1 text-xs font-medium text-white"
                    >
                      <Check className="h-3.5 w-3.5" /> Use this
                    </button>
                    <button
                      onClick={() => setSuggestion(null)}
                      className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs"
                    >
                      <X className="h-3.5 w-3.5" /> Discard
                    </button>
                    <span className="ml-auto text-[11px] text-muted-foreground">{suggestion.model}</span>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {pending.length > 0 && (
          <div className="rounded-lg border border-border bg-card p-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold">Unsaved changes</h3>
              <button
                onClick={() => setDrafts({})}
                className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
              >
                <RotateCcw className="h-3 w-3" /> Revert all
              </button>
            </div>
            <ul className="mt-2 space-y-2">
              {pending.map((c) => (
                <li key={keyOf(c.paragraph_index, c.span_index)} className="text-xs">
                  <span className="text-muted-foreground">¶{c.paragraph_index}</span>
                  <div className="mt-0.5 line-through opacity-60">{c.before.slice(0, 90)}</div>
                  <div className="text-foreground">{c.text.slice(0, 90)}</div>
                </li>
              ))}
            </ul>
            <p className="mt-3 border-t border-border pt-2 text-[11px] text-muted-foreground">
              Submitting writes a new version. The one you opened is kept.
            </p>
          </div>
        )}
      </aside>
    </div>
  );
}
