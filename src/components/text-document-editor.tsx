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
import { AnimatePresence, motion } from "framer-motion";
import { toast } from "sonner";
import { Check, Loader2, RotateCcw, Sparkles, Undo2, X } from "lucide-react";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { DUR, EASE_OUT, SPRING_UI, staggerDelay, useReducedMotionFlag } from "@/components/motion";
import { SkeletonBar } from "@/components/skeletons";
import { ErrorBanner } from "@/components/error-banner";
import { plainly } from "@/components/processing-banner";

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

/* ---------------------------------------------------------------------------
   Run classification.

   `role` is the colour the pre-scanner read out of the .docx, and it has been
   arriving on every span since this endpoint was written -- declared in the type
   above, and then thrown away. It is the same contract the template authoring
   side runs on: blue was a placeholder, red was an instruction to whoever writes
   the letter, black is the template's own prose.

   The labels below are deliberately NOT the template vocabulary. On this screen
   the document is a finished letter, so a blue run is not a placeholder waiting
   to be filled -- it is the value that was filled in. Calling it a placeholder
   here would tell a reviewer the letter is unfinished when it is not.

   Static runs keep `text-foreground` rather than `text-run-static`. The static
   token is a muted grey, correct for de-emphasising boilerplate in an authoring
   tool and wrong here: static text is most of a letter, and greying out most of
   a letter somebody is proof-reading trades readability for a colour that says
   nothing they need. The token still identifies the class -- on the legend
   swatch and on the run's own hover ring -- so all three roles are drawn from
   one ramp.
   --------------------------------------------------------------------------- */

const ROLE: Record<string, { label: string; meaning: string; text: string; swatch: string; ring: string }> = {
  blue: {
    label: "Filled value",
    meaning: "written in when the letter was produced",
    text: "text-run-placeholder",
    swatch: "bg-run-placeholder",
    ring: "hover:ring-run-placeholder/40",
  },
  red: {
    label: "Instruction text",
    meaning: "guidance carried over from the template",
    text: "text-run-instruction",
    swatch: "bg-run-instruction",
    ring: "hover:ring-run-instruction/40",
  },
  black: {
    label: "Static text",
    meaning: "the template's own words",
    text: "text-foreground",
    swatch: "bg-run-static",
    ring: "hover:ring-run-static/40",
  },
};

/** Legend order, and the order roles are looked up in. Anything the server sends
 *  that is not one of these three is rendered as plain text and left out of the
 *  legend entirely -- a colour key for a colour nobody can see is a claim about
 *  the document that the document does not support. */
const ROLE_ORDER = ["blue", "red", "black"] as const;

/** One shape for a run. Hover lifts a pixel on a ring rather than on a border,
 *  so nothing in the paragraph reflows when the pointer crosses it. */
const RUN = cn(
  "rounded px-0.5 text-left align-baseline",
  "transition-[transform,background-color,box-shadow,color] duration-150 ease-out",
  "hover:-translate-y-px hover:ring-1",
);

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
  const reduced = useReducedMotionFlag();

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

  /** The legend is built from the document in front of the reviewer, not from
   *  the list of roles that exist. A letter with no instruction runs left in it
   *  must not carry a key entry for red, because that would have somebody
   *  hunting the page for a colour that is not on it. */
  const rolesPresent = useMemo(() => {
    const seen = new Set<string>();
    for (const p of paragraphs ?? []) {
      for (const s of p.spans) if (ROLE[s.role]) seen.add(s.role);
    }
    return ROLE_ORDER.filter((r) => seen.has(r));
  }, [paragraphs]);

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
      toast.error("Could not get a suggestion", { description: plainly(e?.message ?? String(e)) });
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
      toast.success(`Saved as version ${res.version_no}`, { description: plainly(res.change_summary ?? "") });
      setDrafts({});
      setSelected(null);
      onSaved?.(res.version_id, res.version_no);
    } catch (e: any) {
      toast.error("Could not save", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setSaving(false);
    }
  }

  if (loadError) {
    return (
      // The server's own words are kept in `detail` so they can be quoted in a
      // support thread, but run past `plainly()` first: the engine names its
      // internal artefact in some of these messages and a reviewer has never
      // heard of it.
      <ErrorBanner
        title="This document could not be opened for editing."
        message="It can still be downloaded and opened in Word."
        detail={plainly(loadError)}
      />
    );
  }
  if (!paragraphs) {
    return (
      <div className="rounded-lg border border-border bg-background p-6 shadow-sm">
        <div className="mx-auto max-w-[70ch] space-y-3">
          <p className="flex items-center gap-2 pb-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Reading the document…
          </p>
          {/* Bars where the paragraphs are about to be, so the letter does not
              jump into place under a spinner that was holding no space at all. */}
          {["w-[92%]", "w-full", "w-[78%]", "w-[86%]", "w-[60%]"].map((w, i) => (
            <SkeletonBar key={i} className={cn("h-3", w)} />
          ))}
        </div>
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
            <AnimatePresence>
              {pending.length > 0 && (
                <motion.div
                  // Amber from the shared ramp rather than a palette colour: an
                  // unsaved edit is the same "wants your attention" state the rest
                  // of the product paints in this hue.
                  className="rounded-full border border-ai-uncertain/35 bg-ai-uncertain/12 px-2.5 py-1 text-xs font-medium text-ai-uncertain"
                  initial={reduced ? false : { opacity: 0, scale: 0.96 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.96 }}
                  transition={reduced ? { duration: 0 } : SPRING_UI}
                >
                  {pending.length} unsaved change{pending.length === 1 ? "" : "s"}
                </motion.div>
              )}
            </AnimatePresence>
            <button
              onClick={save}
              disabled={!pending.length || saving}
              className={cn(
                "inline-flex items-center gap-2 rounded-md bg-brand px-3 py-1.5 text-sm font-medium text-white",
                "transition-[transform,box-shadow,opacity] duration-150 ease-out",
                "enabled:hover:glow-brand enabled:active:scale-[0.98] disabled:opacity-40",
              )}
            >
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
              Submit changes
            </button>
          </div>
        </div>

        {rolesPresent.length > 0 && (
          <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] text-muted-foreground">
            {rolesPresent.map((r) => (
              <span key={r} className="flex items-center gap-1.5">
                <span className={cn("h-2 w-2 shrink-0 rounded-full", ROLE[r].swatch)} aria-hidden />
                <span className="font-medium text-foreground/80">{ROLE[r].label}</span>
                <span className="text-muted-foreground">— {ROLE[r].meaning}</span>
              </span>
            ))}
          </div>
        )}

        {/* `doc-surface` switches selection to the document colour: highlighting
            words in here is not the same act as highlighting words in the panel,
            because these carry coordinates the fill engine addresses. */}
        <div className="doc-surface max-h-[70vh] overflow-y-auto rounded-lg border border-border bg-background p-6 shadow-sm">
          <div className="mx-auto max-w-[70ch] space-y-1">
            {paragraphs.map((p) => {
              if (!p.spans.length) return <div key={p.paragraph_index} className="h-3" aria-hidden />;
              return (
                <p key={p.paragraph_index} className={p.in_table ? "text-sm" : ""}>
                  {p.spans.map((s) => {
                    const draft = drafts[keyOf(s.paragraph_index, s.span_index)];
                    const changed = draft !== undefined && draft !== s.text;
                    const active = !!selected
                      && selected.paragraph_index === s.paragraph_index
                      && selected.span_index === s.span_index;
                    const role = ROLE[s.role];

                    const className = cn(
                      RUN,
                      role?.text ?? "text-foreground",
                      role?.ring ?? "hover:ring-border",
                      active
                        ? "bg-brand/20 ring-1 ring-brand"
                        : "hover:bg-accent",
                      // A changed run keeps its role colour and takes the amber
                      // underline on top, so classification and edit state can be
                      // read off the same word at once.
                      changed && "bg-ai-uncertain/15 underline decoration-ai-uncertain decoration-2",
                    );
                    const title = [
                      `Paragraph ${s.paragraph_index}, run ${s.span_index}`,
                      role?.label,
                    ].filter(Boolean).join(" · ");
                    const body = textOf(s) || <span className="text-muted-foreground">(empty)</span>;

                    // Only the selected run is a motion element. Swapping element
                    // type on selection re-mounts it, which is what plays the
                    // scale-in -- and it keeps a five-page letter from paying for
                    // several hundred animation contexts that never animate.
                    // Focus is not lost by the swap: `select()` moves it to the
                    // panel's textarea in the same frame.
                    if (active && !reduced) {
                      return (
                        <motion.button
                          key={s.span_index}
                          onClick={() => select(s)}
                          className={className}
                          title={title}
                          aria-pressed
                          initial={{ scale: 0.96 }}
                          animate={{ scale: 1 }}
                          transition={{ duration: 0.25, ease: EASE_OUT }}
                        >
                          {body}
                        </motion.button>
                      );
                    }
                    return (
                      <button
                        key={s.span_index}
                        onClick={() => select(s)}
                        className={className}
                        title={title}
                        aria-pressed={active}
                      >
                        {body}
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
        <div className="rounded-lg surface-raised p-4">
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
                <label className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
                  Selected — paragraph {selected.paragraph_index}
                  {ROLE[selected.role] && (
                    <span className="flex items-center gap-1">
                      <span
                        className={cn("h-1.5 w-1.5 rounded-full", ROLE[selected.role].swatch)}
                        aria-hidden
                      />
                      {ROLE[selected.role].label}
                    </span>
                  )}
                </label>
                <textarea
                  ref={areaRef}
                  value={textOf(selected)}
                  onChange={(e) => setDraft(selected, e.target.value)}
                  rows={5}
                  className="mt-1 w-full resize-y rounded-md border border-border bg-background p-2 text-sm transition-shadow duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
                />
                <p className="mt-1 text-[11px] text-muted-foreground">
                  Line breaks and tabs cannot be saved into a run — Word stores those as elements,
                  so they would vanish. Edit one line at a time.
                </p>
                {drafts[keyOf(selected.paragraph_index, selected.span_index)] !== undefined && (
                  <button
                    onClick={() => revert(selected)}
                    className="mt-1 inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
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
                    className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1.5 text-sm transition-shadow duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
                  />
                  <button
                    onClick={ask}
                    disabled={asking || !instruction.trim()}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-sm",
                      "transition-[transform,background-color] duration-150 ease-out",
                      "enabled:hover:bg-accent enabled:active:scale-[0.98] disabled:opacity-40",
                    )}
                  >
                    {asking
                      ? <Loader2 className="h-4 w-4 animate-spin text-ai-active" />
                      : <Sparkles className="h-4 w-4" />}
                    Ask
                  </button>
                </div>
              </div>

              <AnimatePresence>
                {suggestion && (
                  <motion.div
                    className="rounded-md border border-brand/40 bg-brand/5 p-3"
                    initial={reduced ? false : { opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: reduced ? 0 : DUR.reveal, ease: EASE_OUT }}
                  >
                    <p className="text-xs font-medium text-brand">Suggested</p>
                    <p className="mt-1 whitespace-pre-wrap text-sm">{suggestion.replacement}</p>
                    {suggestion.note && (
                      <p className="mt-2 border-t border-brand/20 pt-2 text-xs text-muted-foreground">
                        {plainly(suggestion.note)}
                      </p>
                    )}
                    <div className="mt-3 flex items-center gap-2">
                      <button
                        onClick={() => {
                          setDraft(selected, suggestion.replacement);
                          setSuggestion(null);
                        }}
                        className="inline-flex items-center gap-1.5 rounded-md bg-brand px-2.5 py-1 text-xs font-medium text-white transition-transform duration-150 ease-out active:scale-[0.98]"
                      >
                        <Check className="h-3.5 w-3.5" /> Use this
                      </button>
                      <button
                        onClick={() => setSuggestion(null)}
                        className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs transition-[transform,background-color] duration-150 ease-out hover:bg-accent active:scale-[0.98]"
                      >
                        <X className="h-3.5 w-3.5" /> Discard
                      </button>
                      <span className="ml-auto text-[11px] text-muted-foreground">{suggestion.model}</span>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          )}
        </div>

        {pending.length > 0 && (
          <div className="rounded-lg surface-raised p-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold">Unsaved changes</h3>
              <button
                onClick={() => setDrafts({})}
                className="inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
              >
                <RotateCcw className="h-3 w-3" /> Revert all
              </button>
            </div>
            <ul className="mt-2 space-y-2">
              {pending.map((c, i) => (
                <motion.li
                  key={keyOf(c.paragraph_index, c.span_index)}
                  className="text-xs"
                  initial={reduced ? false : { opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{
                    duration: reduced ? 0 : DUR.base,
                    ease: EASE_OUT,
                    delay: reduced ? 0 : staggerDelay(i),
                  }}
                >
                  <span className="text-muted-foreground">¶{c.paragraph_index}</span>
                  <div className="mt-0.5 line-through opacity-60">{c.before.slice(0, 90)}</div>
                  <div className="text-foreground">{c.text.slice(0, 90)}</div>
                </motion.li>
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
