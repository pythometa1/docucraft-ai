import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";

import {
  CAP,
  DUR,
  EASE_IN_OUT,
  EASE_OUT,
  SPRING_PROGRESS,
  SPRING_UI,
  staggerDelay,
  useCountUp,
  useElapsed,
  usePageVisible,
  useReducedMotionFlag,
} from "@/components/motion";
import { ErrorBanner } from "@/components/error-banner";
import { ProcessingMark, STAGE_KIND, plainly, stageLabel } from "@/components/processing-banner";
import type { Stage } from "@/components/compile-progress";
import { cn } from "@/lib/utils";

/**
 * Reading a template, watched.
 *
 * This is the one screen in the product where the machine's work is the
 * content. Everything on it is published by the compiler -- the stages come off
 * the progress feed while the request is in flight, the figures come off the
 * response body when it lands -- and nothing here is derived from a guess. Two
 * consequences of that rule shaped the whole component:
 *
 *  - **The stage rows are the server's stages, verbatim.** No invented
 *    "classifying runs" or "indexing merge fields" steps. The compiler already
 *    reports those numbers as the *result* of the parse stage, so they are
 *    rendered as that row's result instead of being staged as fiction.
 *  - **Nothing renders before its data exists.** The document strip has no bars
 *    until a paragraph count has actually been reported, the figures do not
 *    exist until the response body does, and a figure with no measurement prints
 *    the reason rather than a zero.
 *
 * The shape of the feed is worth stating because it is not a timeline. `agentic`
 * is appended while its children (`chunk`, `write`, `reconcile`, `review`) are
 * still to come, and stays `running` until after they have all finished -- so a
 * flat list ordered by arrival shows a parent that never completes above
 * children that already have. They are nested here because that is the true
 * structure, and `review` repeats up to twelve times, which is also why rows are
 * keyed on their position in the feed rather than on `key`.
 */

/* ------------------------------- the feed ------------------------------- */

/** Stages the compiler appends *underneath* `agentic` rather than after it. */
const AGENTIC_CHILDREN = new Set(["chunk", "write", "reconcile", "review"]);

interface StageNode {
  stage: Stage;
  /** Position in the feed. The React key: `key` repeats, position does not. */
  index: number;
  children: StageNode[];
}

function buildStageTree(stages: Stage[]): StageNode[] {
  const roots: StageNode[] = [];
  let parent: StageNode | null = null;

  for (let index = 0; index < stages.length; index += 1) {
    const stage = stages[index];
    const node: StageNode = { stage, index, children: [] };
    // Only nest under an `agentic` that has actually been published. If the feed
    // ever drops it, its children stay top-level rather than disappearing.
    if (parent !== null && AGENTIC_CHILDREN.has(stage.key)) {
      parent.children.push(node);
      continue;
    }
    roots.push(node);
    if (stage.key === "agentic") parent = node;
  }

  return roots;
}

/**
 * The parse stage reports `"{N} paragraphs, {M} marked runs, {K} merge fields"`.
 * Pulling the three numbers out lets them be set as figures instead of a
 * sentence -- but the format is the server's prose, not a contract, so a miss
 * falls back to printing the string it was given.
 */
const PARSE_DETAIL =
  /^\s*(\d+)\s+paragraphs?\s*,\s*(\d+)\s+marked runs?\s*,\s*(\d+)\s+merge fields?\s*$/i;

interface ParseCounts {
  paragraphs: number;
  markedRuns: number;
  mergeFields: number;
}

function readParseDetail(detail: string | null | undefined): ParseCounts | null {
  const match = detail ? PARSE_DETAIL.exec(detail) : null;
  if (!match) return null;
  return {
    paragraphs: Number(match[1]),
    markedRuns: Number(match[2]),
    mergeFields: Number(match[3]),
  };
}

/* ------------------------------ the response ----------------------------- */

/**
 * Everything the finished response says, and nothing it does not.
 *
 * A `null` on any count means *not reported* and is rendered as a reason, never
 * as a zero -- the two are different claims, and only one of them is ours to
 * make. `readCompiled` returns null outright for a body that is not a compiled
 * reading, which is the case on the authoring path: that endpoint hands back the
 * editable document rather than the reading behind it, so the figures simply do
 * not appear there.
 */
interface Compiled {
  fields: number | null;
  conditions: number | null;
  /** Paragraphs in the document, as counted by the scan. */
  paragraphs: number | null;
  /** Distinct paragraphs inside at least one conditional block. */
  governed: number | null;
  /** Distinct MERGEFIELD codes the reading resolved to a field. */
  mergefieldsResolved: number | null;
  /** MERGEFIELDs the scan saw in the document. */
  mergefieldsSeen: number | null;
  placeholderRows: Set<number>;
  conditionalRows: Set<number>;
  failedReason: string | null;
  didFail: boolean;
  /** The compiler's own answer to "did this need a model?". */
  efficiency: { fellShort: boolean; reason: string | null } | null;
}

function readCompiled(result: any): Compiled | null {
  if (!result || typeof result !== "object" || !Array.isArray(result.fields)) return null;

  const prescan = (result.prescan_summary ?? {}) as Record<string, any>;
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);

  const placeholderRows = new Set<number>();
  const codes = new Set<string>();
  for (const field of result.fields as any[]) {
    for (const slot of (field?.slots ?? []) as any[]) {
      if (typeof slot?.paragraph_index === "number") placeholderRows.add(slot.paragraph_index);
      if (slot?.kind === "mergefield" && typeof slot?.code === "string" && slot.code.trim()) {
        codes.add(slot.code.trim());
      }
    }
  }

  // Paragraphs covered by a conditional block, as a union rather than a count of
  // blocks: blocks overlap, and "4 blocks" says nothing about how much of the
  // document is conditional, which is the thing a reviewer is deciding about.
  const conditionalRows = new Set<number>();
  const blocks = Array.isArray(result.blocks) ? (result.blocks as any[]) : null;
  for (const block of blocks ?? []) {
    const start = block?.start_paragraph;
    const end = block?.end_paragraph;
    if (typeof start !== "number" || typeof end !== "number") continue;
    for (let p = Math.min(start, end); p <= Math.max(start, end); p += 1) conditionalRows.add(p);
  }

  const log = Array.isArray(prescan.agent_log) ? (prescan.agent_log as any[]) : [];
  const scan = log.find((entry) => entry?.stage === "scan");
  const efficiency =
    scan && typeof scan.rules_would_have_fallen_short === "boolean"
      ? {
          fellShort: scan.rules_would_have_fallen_short as boolean,
          reason:
            typeof scan.rule_shortfall_reason === "string" ? scan.rule_shortfall_reason : null,
        }
      : null;

  const notes = Array.isArray(prescan.notes)
    ? (prescan.notes as any[]).filter(
        (n): n is string => typeof n === "string" && n.trim().length > 0,
      )
    : [];
  const didFail = result.status === "failed";

  return {
    fields: result.fields.length,
    conditions: Array.isArray(result.conditions) ? result.conditions.length : null,
    paragraphs: num(prescan.paragraph_count),
    governed: blocks ? conditionalRows.size : null,
    mergefieldsResolved: codes.size,
    mergefieldsSeen: num(prescan.mergefields),
    placeholderRows,
    conditionalRows,
    failedReason: didFail ? (notes[0] ?? null) : null,
    didFail,
    efficiency,
  };
}

/* -------------------------------- the panel ------------------------------- */

export interface CompileRevealProps {
  stages: Stage[];
  failed?: string | null;
  /** The compile response body, once it has landed. Undefined until then, and
   *  undefined for good on callers that never capture it -- which is why every
   *  figure below is gated on it rather than defaulted. */
  result?: any;
  title?: string;
  className?: string;
}

export function CompileReveal({
  stages,
  failed,
  result,
  title = "Processing your template",
  className,
}: CompileRevealProps) {
  const reduced = useReducedMotionFlag();
  const visible = usePageVisible();

  const compiled = useMemo(() => readCompiled(result), [result]);
  const tree = useMemo(() => buildStageTree(stages), [stages]);

  const running = stages.some((s) => s.status === "running");
  const finished = stages.filter((s) => s.status === "done").length;
  const elapsed = useElapsed(running);
  // The deepest thing in flight, not the first: children are appended after
  // their parent, so the last running row is the one actually doing work.
  const active = [...stages].reverse().find((s) => s.status === "running");

  const parse = useMemo(
    () => readParseDetail(stages.find((s) => s.key === "parse")?.detail),
    [stages],
  );

  // The paragraph count arrives twice from two real places: the parse stage says
  // it while the compile is still running, and the response repeats it at the
  // end. Taking the live one first is what lets the document strip exist during
  // the scan rather than appearing after it is over. Neither is derived.
  const paragraphs = compiled?.paragraphs ?? parse?.paragraphs ?? null;

  const failure = failed ?? compiled?.failedReason ?? null;
  const didFail = Boolean(failed) || Boolean(compiled?.didFail);
  const state = didFail ? "failed" : running ? "running" : "done";

  // A failed compile still returns a body, and that body still has a `fields`
  // array -- an empty one. Reporting "Fields: none found" off the back of a run
  // that stopped would be stating a finding where there was no finding, so the
  // figures and the document lighting are withheld and the reason is shown
  // instead. The paragraph count survives, because the scan really did count
  // them before anything went wrong.
  const found = compiled && !compiled.didFail ? compiled : null;

  if (!stages.length && !didFail && !compiled) return null;

  return (
    <motion.section
      initial={reduced ? { opacity: 0 } : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: DUR.reveal, ease: EASE_OUT }}
      className={cn(
        "relative overflow-hidden rounded-2xl border p-4 sm:p-5",
        state === "failed"
          ? "border-ai-blocked/40 bg-ai-blocked/[0.06]"
          : state === "done"
            ? "border-ai-confident/35 bg-ai-confident/[0.05]"
            : "border-ai-active/30 bg-surface-elevated/40 glow-ai",
        className,
      )}
    >
      <span
        aria-hidden
        className="grid-noise pointer-events-none absolute inset-0 opacity-[0.35]"
      />

      <div className="relative">
        <header className="flex items-start gap-3.5">
          <ProcessingMark state={state} reduced={reduced} />

          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
              <h3 className="text-[14px] font-semibold tracking-tight">
                {state === "failed"
                  ? "Processing stopped"
                  : state === "done"
                    ? "Template processed"
                    : title}
              </h3>
              {state === "running" && <RunningPip reduced={reduced} visible={visible} />}
            </div>

            <p className="mt-1 text-[12.5px] leading-relaxed text-muted-foreground">
              {state === "failed"
                ? "Nothing was saved. Your original file is untouched."
                : active
                  ? stageLabel(active)
                  : compiled
                    ? "Here is what it found."
                    : "Everything below finished."}
            </p>
          </div>

          {stages.length > 0 && (
            <div className="shrink-0 text-right">
              <div className="font-mono text-[13px] tabular-nums text-foreground">
                {finished}/{stages.length}
              </div>
              <div className="mt-0.5 text-[10.5px] uppercase tracking-wide text-muted-foreground">
                {state === "running" ? clock(elapsed) : "steps"}
              </div>
            </div>
          )}
        </header>

        <div className="mt-4 flex items-stretch gap-4 sm:gap-5">
          <StageOrFigures
            tree={tree}
            stages={stages}
            parse={parse}
            compiled={found}
            reduced={reduced}
          />

          <DocumentScan
            paragraphs={paragraphs}
            compiled={found}
            scanning={state === "running"}
            reduced={reduced}
            className="hidden sm:flex"
          />
        </div>

        {/* Rendered off the failure itself rather than off having a sentence to
            print: a run that stopped without recording a reason is still a run
            that stopped, and silence there would leave a header saying
            "Processing stopped" with nothing under it. */}
        {didFail && (
          <ErrorBanner
            className="mt-4"
            title="This template could not be read"
            message="Nothing was saved and your uploaded file is untouched."
            detail={failure ? plainly(failure) : undefined}
          />
        )}
      </div>
    </motion.section>
  );
}

/**
 * The swap: stage list out, figures in.
 *
 * Both live in the same grid cell so the cross-fade overlaps rather than
 * queueing, and the cell's floor is pinned to the height the stage list last
 * measured before it left. Pinning a `min-height` once is not the same thing as
 * animating a height -- nothing here transitions a layout property, the number
 * is simply latched so the panel cannot collapse under the outgoing list and
 * shunt everything below it up the page mid-fade.
 */
function StageOrFigures({
  tree,
  stages,
  parse,
  compiled,
  reduced,
}: {
  tree: StageNode[];
  stages: Stage[];
  parse: ParseCounts | null;
  compiled: Compiled | null;
  reduced: boolean;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  const lastListHeight = useRef(0);
  const [floor, setFloor] = useState(0);

  // Deliberately dependency-free: it records the live height of the stage list
  // on every render it is still on screen, so the value latched at swap time is
  // the one the user was looking at a frame earlier.
  useEffect(() => {
    if (listRef.current) lastListHeight.current = listRef.current.offsetHeight;
  });

  // Latched on the way in and released on the way out: this component is mounted
  // once and reused for the next run on some screens, and a floor left behind
  // from the last compile would pad the top of the new one's stage list.
  useEffect(() => {
    if (compiled) {
      if (floor === 0) setFloor(lastListHeight.current);
    } else if (floor !== 0) {
      setFloor(0);
    }
  }, [compiled, floor]);

  const enter = reduced ? { opacity: 0 } : { opacity: 0, y: 6 };
  const leave = reduced ? { opacity: 0 } : { opacity: 0, y: -6 };

  return (
    <div className="grid min-w-0 flex-1" style={{ minHeight: floor || undefined }}>
      <AnimatePresence initial={false}>
        {compiled ? (
          <motion.div
            key="figures"
            style={{ gridArea: "1 / 1" }}
            initial={enter}
            animate={{ opacity: 1, y: 0 }}
            exit={leave}
            transition={{ duration: DUR.reveal, ease: EASE_OUT }}
          >
            <Figures compiled={compiled} reduced={reduced} />
          </motion.div>
        ) : (
          <motion.div
            key="stages"
            ref={listRef}
            style={{ gridArea: "1 / 1" }}
            initial={false}
            exit={leave}
            transition={{ duration: DUR.base, ease: EASE_OUT }}
          >
            <StageSegments stages={stages} reduced={reduced} />
            <StageTree nodes={tree} parse={parse} reduced={reduced} className="mt-3.5" />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/** One segment per published stage. Discrete because the work is discrete: a
 *  smooth bar would be inventing a resolution the compiler does not report. */
function StageSegments({ stages, reduced }: { stages: Stage[]; reduced: boolean }) {
  return (
    <div className="flex gap-1" aria-hidden>
      {stages.map((s, i) => (
        <motion.span
          key={i}
          className={cn(
            "h-1 flex-1 rounded-full",
            s.status === "done"
              ? "bg-ai-confident/70"
              : s.status === "failed"
                ? "bg-ai-blocked/80"
                : s.status === "running"
                  ? "ai-skeleton"
                  : "bg-border/70",
          )}
          initial={reduced ? { opacity: 0 } : { scaleX: 0.4, opacity: 0 }}
          animate={{ scaleX: 1, opacity: 1 }}
          transition={{ duration: DUR.base, ease: EASE_OUT, delay: reduced ? 0 : staggerDelay(i) }}
          style={{ transformOrigin: "left" }}
        />
      ))}
    </div>
  );
}

function StageTree({
  nodes,
  parse,
  reduced,
  className,
  nested = false,
}: {
  nodes: StageNode[];
  parse: ParseCounts | null;
  reduced: boolean;
  className?: string;
  nested?: boolean;
}) {
  return (
    <ol className={cn("space-y-2.5", className)}>
      <AnimatePresence initial={false}>
        {nodes.map((node) => (
          <motion.li
            key={node.index}
            initial={reduced ? { opacity: 0 } : { opacity: 0, x: -4 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: DUR.base, ease: EASE_OUT }}
          >
            <StageRow node={node} parse={parse} reduced={reduced} nested={nested} />

            {node.children.length > 0 && (
              // The indent is the parent/child relationship made visible. The
              // guide rail runs the height of the children so an `agentic` row
              // that is still running reads as "these are what it is doing",
              // not as "this one is stuck".
              <StageTree
                nodes={node.children}
                parse={parse}
                reduced={reduced}
                nested
                className="ml-2.5 mt-2.5 border-l border-border/70 pl-3.5"
              />
            )}
          </motion.li>
        ))}
      </AnimatePresence>
    </ol>
  );
}

function StageRow({
  node,
  parse,
  reduced,
  nested,
}: {
  node: StageNode;
  parse: ParseCounts | null;
  reduced: boolean;
  nested: boolean;
}) {
  const { stage } = node;
  const kind = STAGE_KIND[stage.kind] ?? STAGE_KIND.deterministic;
  const Icon = kind.icon;
  const counts = stage.key === "parse" ? parse : null;

  return (
    <div className="flex items-start gap-2.5">
      <span
        className={cn(
          "mt-px flex shrink-0 items-center justify-center rounded-full border",
          nested ? "h-4 w-4" : "h-5 w-5",
          stage.status === "done"
            ? "border-ai-confident/50 bg-ai-confident/15 text-ai-confident"
            : stage.status === "failed"
              ? "border-ai-blocked/50 bg-ai-blocked/15 text-ai-blocked"
              : cn(kind.ring, kind.hue),
        )}
      >
        {stage.status === "done" ? (
          <DrawnCheck reduced={reduced} className={nested ? "h-2.5 w-2.5" : "h-3 w-3"} />
        ) : (
          <Icon
            className={cn(
              nested ? "h-2.5 w-2.5" : "h-3 w-3",
              stage.status === "running" && !reduced && "animate-pulse",
            )}
          />
        )}
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <span
            className={cn(
              "leading-snug",
              nested ? "text-[12px]" : "text-[12.5px]",
              stage.status === "running"
                ? "font-medium text-foreground"
                : stage.status === "done"
                  ? "text-muted-foreground"
                  : "text-foreground",
            )}
          >
            {stageLabel(stage)}
          </span>

          {/* Naming the kind is the point of the list. The model rows are the
              ones that cost money and minutes, and which one a run is sitting
              in should never be a guess. */}
          <span
            className={cn(
              "inline-flex items-center gap-1 text-[10px] uppercase tracking-wide",
              stage.status === "done" ? "text-muted-foreground/70" : kind.hue,
            )}
          >
            <Icon className="h-2.5 w-2.5" />
            {kind.label}
          </span>
        </div>

        {/* The row's own result, underneath it. This is what makes the list
            narrate rather than tick: the compiler already answers "what did that
            step find?" and the answer belongs against the step that found it. */}
        {counts ? (
          <ParseFigures counts={counts} reduced={reduced} />
        ) : (
          stage.detail && (
            <div className="mt-0.5 text-[11.5px] leading-relaxed text-muted-foreground">
              {plainly(stage.detail)}
            </div>
          )
        )}
      </div>

      {stage.status === "running" && (
        <span className="ai-skeleton mt-1.5 h-1 w-12 shrink-0 rounded-full" />
      )}
    </div>
  );
}

/** The parse stage's three numbers, set as figures. Rendered only when the
 *  sentence matched; the caller prints the raw string when it did not. */
function ParseFigures({ counts, reduced }: { counts: ParseCounts; reduced: boolean }) {
  const items: [string, number][] = [
    ["paragraphs", counts.paragraphs],
    ["marked runs", counts.markedRuns],
    ["merge fields", counts.mergeFields],
  ];

  return (
    <div className="mt-1.5 flex flex-wrap gap-x-5 gap-y-1.5">
      {items.map(([label, value], i) => (
        <motion.span
          key={label}
          className="inline-flex items-baseline gap-1.5"
          initial={reduced ? { opacity: 0 } : { opacity: 0, y: 3 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: DUR.base, ease: EASE_OUT, delay: staggerDelay(i) }}
        >
          <span className="font-mono text-[13px] font-medium tabular-nums text-foreground">
            {value}
          </span>
          <span className="text-[10.5px] uppercase tracking-wide text-muted-foreground">
            {label}
          </span>
        </motion.span>
      ))}
    </div>
  );
}

/* -------------------------------- figures -------------------------------- */

function Figures({ compiled, reduced }: { compiled: Compiled; reduced: boolean }) {
  // Shown only when the ratio is a subset of a like-for-like total. Resolved
  // merge fields are distinct codes and the scan's figure counts occurrences, so
  // the two can legitimately disagree -- and "9 of 4" is worse than no
  // denominator at all.
  const mergefieldTotal =
    compiled.mergefieldsSeen != null &&
    compiled.mergefieldsResolved != null &&
    compiled.mergefieldsResolved <= compiled.mergefieldsSeen
      ? compiled.mergefieldsSeen
      : null;
  const governedTotal =
    compiled.paragraphs != null &&
    compiled.governed != null &&
    compiled.governed <= compiled.paragraphs
      ? compiled.paragraphs
      : null;

  return (
    <div>
      <div className="grid grid-cols-2 gap-2.5">
        <Figure
          reduced={reduced}
          index={0}
          label="Fields"
          value={compiled.fields}
          empty="None found"
        />
        <Figure
          reduced={reduced}
          index={1}
          label="Conditions"
          value={compiled.conditions}
          empty="None found"
        />
        <Figure
          reduced={reduced}
          index={2}
          label="Paragraphs governed"
          value={compiled.governed}
          total={governedTotal}
          empty="No conditional sections"
        />
        <Figure
          reduced={reduced}
          index={3}
          label="Merge fields resolved"
          value={compiled.mergefieldsResolved}
          total={mergefieldTotal}
          // "None resolved" and "none there to resolve" are different facts and
          // the scan already knows which one this is.
          empty={compiled.mergefieldsSeen === 0 ? "None in this document" : "None resolved"}
        />
      </div>

      {compiled.efficiency && (
        <EfficiencyBadge efficiency={compiled.efficiency} reduced={reduced} />
      )}
    </div>
  );
}

/**
 * One figure.
 *
 * Three states, and keeping them apart is the whole job. A measured number
 * counts up. A measured *nothing* prints a word, because a counter animating to
 * zero is a small piece of theatre that says "we looked hard and found none"
 * with more confidence than a zero deserves. An absent measurement prints why it
 * is absent, and never a number.
 */
function Figure({
  label,
  value,
  total,
  empty,
  missing = "Not reported for this run",
  index,
  reduced,
}: {
  label: string;
  value: number | null;
  total?: number | null;
  empty: string;
  missing?: string;
  index: number;
  reduced: boolean;
}) {
  const shown = useCountUp(value ?? 0, 700, value != null && value > 0);

  return (
    <motion.div
      initial={reduced ? { opacity: 0 } : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ ...SPRING_PROGRESS, delay: reduced ? 0 : staggerDelay(index) }}
      className="rounded-xl border border-border/70 bg-surface/60 px-3 py-2.5"
    >
      {/* A fixed floor on the value line, so a tile printing a sentence and a
          tile printing a number are the same height and the grid does not
          stagger. */}
      <div className="flex min-h-[24px] items-end">
        {value == null ? (
          <p className="text-[11.5px] leading-snug text-muted-foreground">{missing}</p>
        ) : value === 0 ? (
          <p className="text-[11.5px] leading-snug text-muted-foreground">{empty}</p>
        ) : (
          <div className="flex items-baseline gap-1.5">
            <span className="font-mono text-[22px] font-semibold leading-none tabular-nums text-foreground">
              {Math.round(shown)}
            </span>
            {total != null && (
              <span className="font-mono text-[12px] tabular-nums text-muted-foreground">
                of {total}
              </span>
            )}
          </div>
        )}
      </div>
      <p className="mt-1.5 text-[10.5px] uppercase tracking-wide text-muted-foreground">{label}</p>
    </motion.div>
  );
}

/**
 * Whether the colour rules alone could have read this document.
 *
 * The badge people ask for here is "read without a model" -- and on this path
 * that state does not exist: every template goes to a model, so a badge claiming
 * otherwise would be decoration with a lie inside it. The compiler does record
 * the honest version of the same question, as a diagnostic on the scan: it runs
 * the deterministic rules anyway and notes whether they would have fallen short.
 * That is the figure worth showing, and it is the one below.
 */
function EfficiencyBadge({
  efficiency,
  reduced,
}: {
  efficiency: { fellShort: boolean; reason: string | null };
  reduced: boolean;
}) {
  return (
    <motion.div
      initial={reduced ? { opacity: 0 } : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ ...SPRING_PROGRESS, delay: reduced ? 0 : staggerDelay(4) }}
      className="mt-2.5"
    >
      <span
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium",
          efficiency.fellShort
            ? "border-border bg-surface/60 text-muted-foreground"
            : "border-ai-confident/40 bg-ai-confident/10 text-ai-confident",
        )}
      >
        <span
          className={cn(
            "h-1.5 w-1.5 rounded-full",
            efficiency.fellShort ? "bg-muted-foreground/70" : "bg-ai-confident",
          )}
        />
        {efficiency.fellShort
          ? "Needed a model to read"
          : "Colour rules alone would have read this template"}
      </span>

      {efficiency.fellShort && efficiency.reason && (
        <p className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
          {plainly(efficiency.reason)}
        </p>
      )}
    </motion.div>
  );
}

/* ----------------------------- document strip ---------------------------- */

/** Bars rendered at most. A five-hundred paragraph contract is a real thing and
 *  five hundred nodes in a dialog is not; past this each bar stands for a range
 *  of paragraphs, and the strip says so underneath. */
const MAX_BARS = 64;

type BarTone = "static" | "placeholder" | "conditional";

/**
 * The document, as the scan sees it: one bar per paragraph, top to bottom.
 *
 * Nothing is drawn until a paragraph count has been reported -- the strip is
 * absent, not estimated, because a plausible-looking wireframe of a document
 * nobody has counted is exactly the kind of fiction this screen must not
 * contain. Once the reading lands the bars light from real indices: a paragraph
 * holding a field slot, a paragraph inside a conditional block, or neither.
 *
 * Fields win over conditional regions where both apply. "There is a field here"
 * is the more specific fact about a paragraph, and it is the one somebody
 * scanning this strip is looking for.
 */
function DocumentScan({
  paragraphs,
  compiled,
  scanning,
  reduced,
  className,
}: {
  paragraphs: number | null;
  compiled: Compiled | null;
  scanning: boolean;
  reduced: boolean;
  className?: string;
}) {
  const visible = usePageVisible();

  const bars = useMemo<BarTone[]>(() => {
    if (!paragraphs || paragraphs <= 0) return [];
    const count = Math.min(paragraphs, MAX_BARS);
    const out: BarTone[] = [];

    for (let i = 0; i < count; i += 1) {
      const from = Math.floor((i * paragraphs) / count);
      const to = Math.max(from, Math.floor(((i + 1) * paragraphs) / count) - 1);
      let tone: BarTone = "static";
      for (let p = from; p <= to; p += 1) {
        if (compiled?.placeholderRows.has(p)) {
          tone = "placeholder";
          break;
        }
        if (compiled?.conditionalRows.has(p)) tone = "conditional";
      }
      out.push(tone);
    }
    return out;
  }, [paragraphs, compiled]);

  if (!bars.length || !paragraphs) return null;

  const perBar = Math.ceil(paragraphs / bars.length);

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: DUR.reveal, ease: EASE_OUT }}
      className={cn("w-[104px] shrink-0 flex-col", className)}
    >
      <div className="relative overflow-hidden rounded-lg border border-border/70 bg-surface/40 p-2">
        <div className="flex flex-col gap-[2px]">
          {bars.map((tone, i) => (
            <span
              key={i}
              className="relative h-[2px] w-full overflow-hidden rounded-full bg-run-static/30"
            >
              {compiled && tone !== "static" && (
                <motion.span
                  className={cn(
                    "absolute inset-0 rounded-full",
                    tone === "placeholder" ? "bg-run-placeholder" : "bg-ai-uncertain",
                  )}
                  style={{ transformOrigin: "left" }}
                  initial={reduced ? { opacity: 1 } : { opacity: 0, scaleX: 0 }}
                  animate={{ opacity: 1, scaleX: 1 }}
                  transition={{
                    duration: DUR.base,
                    ease: EASE_OUT,
                    // 20ms apart, and the whole strip lands inside the sequence
                    // budget however many bars there are.
                    delay: reduced ? 0 : Math.min((i * 20) / 1000, CAP.sequenceMs / 1000),
                  }}
                />
              )}
            </span>
          ))}
        </div>

        {/* The scan line. A band rather than a hairline so the travel can be
            expressed as a percentage of its own height -- a transform, never a
            `top`. It stops when the work does, and when the tab is not in
            front. */}
        {scanning && !reduced && visible && (
          <motion.span
            aria-hidden
            className="pointer-events-none absolute inset-x-0 top-0 h-[18%] bg-gradient-to-b from-transparent via-ai-active/25 to-transparent"
            animate={{ y: ["-110%", "560%"] }}
            transition={{ duration: 2.4, repeat: Infinity, ease: EASE_IN_OUT }}
          />
        )}
      </div>

      <p className="mt-2 text-[10px] leading-relaxed text-muted-foreground">
        {perBar > 1
          ? `${paragraphs} paragraphs, up to ${perBar} per bar`
          : `${paragraphs} paragraphs`}
      </p>

      {/* A key for the colours that are actually on the strip. Listing a swatch
          nothing is painted in invites the reader to hunt for it. */}
      {compiled && (
        <ul className="mt-1.5 space-y-1">
          {bars.includes("placeholder") && <ScanKey tone="bg-run-placeholder" label="Field" />}
          {bars.includes("conditional") && <ScanKey tone="bg-ai-uncertain" label="Conditional" />}
          <ScanKey tone="bg-run-static/60" label="Unchanged" />
        </ul>
      )}
    </motion.div>
  );
}

function ScanKey({ tone, label }: { tone: string; label: string }) {
  return (
    <li className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
      <span className={cn("h-[2px] w-4 rounded-full", tone)} />
      {label}
    </li>
  );
}

/* -------------------------------- fragments ------------------------------- */

/** The check that draws itself. `pathLength` is a transform of the stroke, not
 *  of the box, so it costs nothing to composite and cannot nudge the row. */
function DrawnCheck({ reduced, className }: { reduced: boolean; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} aria-hidden>
      <motion.path
        d="M4 12.5 L9.5 18 L20 6.5"
        stroke="currentColor"
        strokeWidth={3}
        strokeLinecap="round"
        strokeLinejoin="round"
        initial={{ pathLength: reduced ? 1 : 0, opacity: reduced ? 1 : 0.4 }}
        animate={{ pathLength: 1, opacity: 1 }}
        transition={SPRING_UI}
      />
    </svg>
  );
}

function RunningPip({ reduced, visible }: { reduced: boolean; visible: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-ai-active/35 bg-ai-active/10 px-2 py-0.5 text-[10.5px] font-medium uppercase tracking-wide text-ai-active">
      <span className="relative flex h-1.5 w-1.5">
        <span className="absolute inline-flex h-full w-full rounded-full bg-ai-active opacity-70" />
        {/* Paused on a hidden tab as well as under reduced motion. A compile can
            run for three minutes, which is exactly when somebody switches away --
            and this file already pauses its scan line on visibility, so the pip
            being the one loop that kept going was an oversight rather than a
            different judgement. */}
        {!reduced && visible && (
          <motion.span
            className="absolute inline-flex h-full w-full rounded-full bg-ai-active"
            animate={{ scale: [1, 2.6], opacity: [0.65, 0] }}
            transition={{ duration: 1.4, repeat: Infinity, ease: "easeOut" }}
          />
        )}
      </span>
      Running
    </span>
  );
}

function clock(total: number) {
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}
