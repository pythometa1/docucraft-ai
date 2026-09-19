/**
 * The authoring guide, for signed-in users.
 *
 * The content comes from the server rather than living here: anything in the
 * bundle can be read without an account, and this page is the detailed one.
 * The page only knows how to draw each kind of block.
 */

import { createFileRoute } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { Check, Copy } from "lucide-react";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { ErrorBanner } from "@/components/error-banner";
import { FadeIn } from "@/components/motion";
import { SkeletonBar } from "@/components/skeletons";
import type { Guide, GuideBlock } from "@/lib/types";

export const Route = createFileRoute("/_app/guide")({
  head: () => ({
    meta: [
      { title: "Guide — DocuMind AI" },
      { name: "description", content: "How to prepare templates and use DocuMind AI." },
    ],
  }),
  component: GuidePage,
});

const METHOD_TONE: Record<string, string> = {
  GET: "bg-info/12 text-info border-info/30",
  POST: "bg-success/12 text-success border-success/30",
  PATCH: "bg-warning/12 text-warning border-warning/30",
  DELETE: "bg-destructive/12 text-destructive border-destructive/30",
};

/** Text with `backtick` spans shown as code, the one inline style the guide uses. */
function Inline({ text }: { text: string }) {
  const parts = text.split("`");
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <code key={i} className="rounded bg-muted px-1 py-0.5 font-mono text-[12px] text-foreground">{part}</code>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}

function CodeBlock({ lang, text }: { lang: string; text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="my-3 overflow-hidden rounded-xl border border-border bg-[color-mix(in_oklab,var(--color-surface)_60%,black_8%)]">
      <div className="flex items-center justify-between border-b border-border/60 px-3 py-1.5">
        <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">{lang}</span>
        <button
          onClick={() => {
            void navigator.clipboard?.writeText(text);
            setCopied(true);
            setTimeout(() => setCopied(false), 1400);
          }}
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          {copied ? <Check className="h-3 w-3 text-success" /> : <Copy className="h-3 w-3" />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="overflow-x-auto px-3.5 py-3 text-[12.5px] leading-relaxed">
        <code className="font-mono text-foreground/90">{text}</code>
      </pre>
    </div>
  );
}

function Block({ block }: { block: GuideBlock }) {
  switch (block.type) {
    case "p":
      return <p><Inline text={block.text} /></p>;
    case "steps":
      return (
        <div className="mt-2 space-y-5">
          {block.items.map((step, i) => (
            <div key={i} className="relative pl-11">
              <div className="absolute left-0 top-0 flex h-7 w-7 items-center justify-center rounded-full bg-gradient-brand font-mono text-xs font-semibold text-white">
                {i + 1}
              </div>
              <div className="text-[14px] font-medium text-foreground">{step.title}</div>
              <div className="mt-1 text-[13.5px] leading-relaxed"><Inline text={step.text} /></div>
            </div>
          ))}
        </div>
      );
    case "swatches":
      return (
        <div className="mt-3 grid gap-2 sm:grid-cols-3">
          {block.items.map((s) => (
            <div key={s.name} className="flex items-start gap-3 rounded-xl border border-border bg-background/40 p-3">
              <span className="mt-0.5 h-4 w-4 shrink-0 rounded" style={{ background: s.colour }} />
              <div className="min-w-0">
                <div className="text-[13.5px] font-medium text-foreground">{s.name}</div>
                <div className="text-[13px]">{s.means}</div>
                <div className="mt-0.5 font-mono text-[11px] text-muted-foreground/70">{s.hexes}</div>
              </div>
            </div>
          ))}
        </div>
      );
    case "highlights":
      return (
        <p>
          <Inline text={block.text} />{" "}
          {block.items.map((h, i) => (
            <span key={h.label}>
              {i > 0 && (i === block.items.length - 1 ? " and " : ", ")}
              <span className="rounded px-1" style={{ background: h.colour, color: "#000" }}>{h.label}</span>{" "}
              {h.means}
            </span>
          ))}
          .
        </p>
      );
    case "note":
      return (
        <p className={cn(
          "rounded-xl border p-3 text-[13.5px]",
          block.tone === "warning" ? "border-warning/30 bg-warning/5" : "border-info/25 bg-info/5",
        )}>
          <strong className={block.tone === "warning" ? "text-warning" : "text-foreground"}>{block.title}</strong>{" "}
          <Inline text={block.text} />
        </p>
      );
    case "code":
      return <CodeBlock lang={block.lang} text={block.text} />;
    case "table":
      return (
        <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full text-left text-[13px]">
            <thead>
              <tr className="border-b border-border/60 text-[11px] uppercase tracking-wider text-muted-foreground">
                {block.columns.map((c) => <th key={c} className="px-3 py-2 font-medium">{c}</th>)}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, i) => (
                <tr key={i} className="border-b border-border/60 last:border-0">
                  {row.map((cell, j) => (
                    <td key={j} className={cn("px-3 py-2 align-top", j === 0 && "font-mono text-[12px] text-foreground")}>
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case "cards":
      return (
        <div className="grid gap-3 sm:grid-cols-2">
          {block.items.map((c) => (
            <div key={c.title} className="rounded-xl border border-border bg-background/40 p-3.5">
              <div className="text-[13.5px] font-medium text-foreground">{c.title}</div>
              <p className="mt-1 text-[13px]"><Inline text={c.text} /></p>
            </div>
          ))}
        </div>
      );
    case "endpoints":
      return (
        <div>
          <h3 className="mb-1 mt-4 text-[14px] font-medium text-foreground">{block.title}</h3>
          <div className="rounded-xl border border-border px-3">
            {block.items.map(([method, path, what]) => (
              <div key={method + path} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-border/60 py-2.5 last:border-0">
                <span className={cn("shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[10px] font-semibold",
                                    METHOD_TONE[method] ?? METHOD_TONE.GET)}>
                  {method}
                </span>
                <code className="font-mono text-[12.5px] text-foreground">{path}</code>
                <span className="w-full text-[13px] sm:w-auto sm:flex-1">{what}</span>
              </div>
            ))}
          </div>
        </div>
      );
    default:
      // A block type this build does not know yet is skipped, not shown raw.
      return null;
  }
}

function GuidePage() {
  const [guide, setGuide] = useState<Guide | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setGuide(await api.guide());
    } catch {
      setError("The guide could not be loaded. Please try again.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  return (
    <div className="mx-auto max-w-[1200px] p-6 lg:p-8">
      <FadeIn className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight text-gradient">{guide?.title ?? "Guide"}</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {guide?.intro ?? "How to prepare templates and use DocuMind AI."}
        </p>
      </FadeIn>

      {error && <ErrorBanner title="Could not load the guide" message={error} onRetry={load} retrying={loading} />}

      {loading && !guide && (
        <div className="space-y-3">
          <SkeletonBar className="h-6 w-1/3" />
          <SkeletonBar className="h-4 w-full" />
          <SkeletonBar className="h-4 w-5/6" />
          <SkeletonBar className="h-4 w-2/3" />
        </div>
      )}

      {guide && (
        <div className="grid grid-cols-1 gap-10 lg:grid-cols-[220px_1fr]">
          <aside className="hidden lg:block">
            <nav className="sticky top-6 space-y-0.5">
              {guide.sections.map((s) => (
                <a key={s.id} href={`#${s.id}`}
                   className="block truncate rounded-lg px-2.5 py-1.5 text-[13px] text-muted-foreground transition-colors hover:bg-accent/50 hover:text-foreground">
                  {s.title}
                </a>
              ))}
            </nav>
          </aside>
          <div className="min-w-0 space-y-12 pb-24">
            {guide.sections.map((s) => (
              <section key={s.id} id={s.id} className="scroll-mt-6">
                {s.eyebrow && (
                  <div className="mb-1 font-mono text-[11px] uppercase tracking-wider text-brand">{s.eyebrow}</div>
                )}
                <h2 className="text-xl font-semibold tracking-tight">{s.title}</h2>
                <div className="mt-3 space-y-3 text-[14px] leading-relaxed text-muted-foreground">
                  {s.blocks.map((b, i) => <Block key={i} block={b} />)}
                </div>
              </section>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
