/**
 * A running batch, and what came out of it.
 *
 * Extracted because the job used to live in `DocumentMapping`'s own state, which
 * made it the only route to three things that exist nowhere else: `job.error`,
 * the per-row `error` and `qa_notes`, and the archive of the run. A stage
 * change, a refresh, or simply pressing Next lost all of them -- and rows that
 * fail outright never produce a document at all, so a failed row left no trace
 * anywhere in the product once this component unmounted.
 *
 * So the job is held by the project screen and the polling lives here, in one
 * hook mounted once. Generation is started on the mapping stage and watched on
 * the documents stage, which is where the reader is going to look for its
 * output.
 *
 * ----------------------------------------------------------------------------
 * What this panel is allowed to draw
 *
 * Everything on screen here is a value the runner published. `GET /jobs/{id}`
 * hands back `status`, `error` and `progress`, and `progress` carries
 * `rows_total`, `rows_done`, the four status counts, `canary_size`, the locale
 * decision, and `rows[]` -- each row a terminal outcome with `status`,
 * `qa_passed`, `qa_notes[]`, `open_tasks`, `error` and `is_canary`.
 *
 * Three things a batch screen wants and this one does not have, so it does not
 * draw them:
 *
 *  - **A per-row "rendering, now verifying" phase.** A row is written to
 *    `progress` once, already finished. So a row has exactly two honest states
 *    here: not reported yet, and the verdict it arrived with.
 *  - **A cursor on the row being worked.** Nothing publishes one, and inventing
 *    "row 41 is rendering now" would be a moving part with no engine behind it.
 *  - **Throughput.** There are no per-row timestamps in the payload, so
 *    documents-per-second would be a number this file made up.
 *
 * The gate, on the other hand, is entirely real: the runner fills and fully
 * checks `canary_size` sample rows, and the remaining rows are attempted only if
 * every one of them passes. That is the mechanism worth animating, so it is
 * drawn as a gate that opens -- and when `status` is `blocked`, one that does
 * not.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, Download, Loader2, Lock, RefreshCw, ShieldCheck } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { SkeletonBar } from "@/components/skeletons";
import { plainly } from "@/components/processing-banner";
import { qaNoteLines } from "@/lib/friendly";
import {
  DUR,
  EASE_OUT,
  SPRING_PROGRESS,
  SPRING_UI,
  staggerDelay,
  useCountUp,
  usePageVisible,
  useReducedMotionFlag,
} from "@/components/motion";
import { cn } from "@/lib/utils";

/** Statuses after which there is nothing left to poll for. */
const TERMINAL_JOB_STATES = ["completed", "completed_with_errors", "failed", "blocked"];

/** Consecutive poll failures tolerated before giving up and saying so. A batch
 *  can take minutes and a single dropped request is not a reason to stop
 *  watching -- but silently retrying forever is how "it is still running" and
 *  "the server stopped answering" become indistinguishable. */
const POLL_TOLERANCE = 2;
const POLL_INTERVAL_MS = 1200;
const ROWS_SHOWN = 6;

/** How many failed rows the collapsed list will draw. Failures used to be
 *  exempt from every cap, on the reasoning that a failed row is the only reason
 *  anybody scrolls this list -- true, and also how a batch that failed all five
 *  thousand of its rows put five thousand animated list items on screen at once.
 *  Nobody reads the five thousandth. What is over the cap is counted and said
 *  out loud rather than dropped quietly, and "Show all" still renders every one
 *  of them. */
const FAILURES_SHOWN = 24;

export type BatchJob = { job_id: string; status: string; error?: string | null; progress?: any };

export type BatchWatch = {
  job: BatchJob | null;
  start: (job: BatchJob) => void;
  clear: () => void;
  /** True while the batch is still running, so a caller can disable things. */
  running: boolean;
  error: string | null;
  stopped: boolean;
  resume: () => void;
};

/** Holds one batch and keeps it up to date. Mount once, high enough up that a
 *  stage change does not unmount it. */
export function useBatchWatch(): BatchWatch {
  const [job, setJob] = useState<BatchJob | null>(null);
  const [tick, setTick] = useState(0);
  const [stopped, setStopped] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const failures = useRef(0);

  const start = useCallback((next: BatchJob) => {
    failures.current = 0;
    setStopped(false);
    setError(null);
    setJob(next);
  }, []);

  const clear = useCallback(() => {
    failures.current = 0;
    setStopped(false);
    setError(null);
    setJob(null);
  }, []);

  const resume = useCallback(() => {
    failures.current = 0;
    setStopped(false);
    setError(null);
    setTick((t) => t + 1);
  }, []);

  // The canary set renders first and has to pass before the rest of the rows are
  // attempted, so an early failure shows up here rather than after a thousand
  // documents.
  useEffect(() => {
    if (!job?.job_id) return;
    if (TERMINAL_JOB_STATES.includes(job.status)) return;
    if (stopped) return;
    const timer = setTimeout(() => {
      // Keep the id we started with. `POST .../generate-batch` answers with
      // `job_id` and `GET /jobs/{id}` answers with `id`, so replacing the whole
      // object on each poll dropped the identifier -- and the download control,
      // which only renders once polling has finished, then asked for
      // `/jobs/undefined/download` and reported the archive as unavailable.
      api.getJob(job.job_id)
        .then((next: any) => {
          failures.current = 0;
          setError(null);
          setJob({ ...next, job_id: job.job_id });
        })
        .catch((e: any) => {
          failures.current += 1;
          const reason = plainly(String(e?.message ?? e));
          if (failures.current > POLL_TOLERANCE) {
            setStopped(true);
            setError(reason);
            toast.error("Lost track of this batch", {
              description: `${reason} The documents may still be finishing on the server — press "Check again".`,
            });
            return;
          }
          // Nothing in `job` changed, so the effect will not re-run on its own:
          // bump the tick to re-arm the timer instead of quietly stopping.
          setError(`${reason} — retrying (${failures.current} of ${POLL_TOLERANCE}).`);
          setTick((t) => t + 1);
        });
    }, POLL_INTERVAL_MS);
    return () => clearTimeout(timer);
  }, [job, stopped, tick]);

  return {
    job,
    start,
    clear,
    running: Boolean(job && !TERMINAL_JOB_STATES.includes(job.status)),
    error,
    stopped,
    resume,
  };
}

/* --------------------------------------------------------------------------
   Vocabulary
   -------------------------------------------------------------------------- */

/** How each terminal state should read. Separated from the markup because the
 *  distinction the old version missed was exactly here: `failed` and `blocked`
 *  are different things and only one of them was being explained.
 *
 *  The tones are the app's intelligence ramp rather than raw palette colours, so
 *  "this is fine" and "this stopped" are the same two colours on this screen as
 *  they are on every other one.
 *
 *  Nothing here says "generating with AI". The batch path resolves and fills on
 *  the server's own rules; the verbs are render, check, QA. */
const OUTCOME: Record<string, {
  tone: string; edge: string; bar: string; heading: string; hint: string;
}> = {
  queued: {
    tone: "text-muted-foreground",
    edge: "border-border",
    bar: "bg-ai-idle",
    heading: "Queued",
    hint: "It will start shortly.",
  },
  running: {
    tone: "text-ai-active",
    edge: "border-ai-active/30",
    bar: "bg-ai-active",
    heading: "Generating",
    hint: "Each row is filled from your data and checked before the next one starts.",
  },
  completed: {
    tone: "text-ai-confident",
    edge: "border-ai-confident/35",
    bar: "bg-ai-confident",
    heading: "Finished",
    hint: "Every row produced a document.",
  },
  completed_with_errors: {
    tone: "text-ai-uncertain",
    edge: "border-ai-uncertain/40",
    bar: "bg-ai-uncertain",
    heading: "Finished, with failures",
    hint: "Some rows did not produce a document. The ones that did are in the project.",
  },
  blocked: {
    tone: "text-ai-blocked",
    edge: "border-ai-blocked/40",
    bar: "bg-ai-blocked",
    heading: "Stopped before it ran",
    hint: "The first documents checked did not pass, so the rest were held back. "
      + "Fix what is listed below and generate again.",
  },
  failed: {
    tone: "text-ai-blocked",
    edge: "border-ai-blocked/40",
    bar: "bg-ai-blocked",
    heading: "Failed",
    hint: "The batch stopped on an error. Nothing further was attempted.",
  },
};

/** What a row's own status means, in the reader's words rather than the
 *  engine's. `generated` is the runner's word for "QA passed and nobody has to
 *  decide anything", which is worth saying out loud -- and `failed` is a row
 *  that never produced a file at all, so it deliberately says nothing about QA:
 *  the checks never ran on it. */
const ROW_STATE: Record<string, { label: string; chip: string }> = {
  generated: {
    label: "Passed checks",
    chip: "border-ai-confident/40 bg-ai-confident/10 text-ai-confident",
  },
  pending_review: {
    label: "Needs a decision",
    chip: "border-ai-uncertain/40 bg-ai-uncertain/10 text-ai-uncertain",
  },
  blocked: {
    label: "Failed checks",
    chip: "border-ai-blocked/40 bg-ai-blocked/10 text-ai-blocked",
  },
  failed: {
    label: "Not produced",
    chip: "border-ai-blocked/40 bg-ai-blocked/10 text-ai-blocked",
  },
};

const UNKNOWN_ROW_STATE = { label: "Reported", chip: "border-border bg-surface text-muted-foreground" };

/** Where the date and number formatting came from, as a sentence. The runner
 *  publishes `locale_source` precisely so a reviewer reading "May 9, 2024" can
 *  tell a configured decision from a default nobody made -- so `default` is
 *  spelled out as the non-decision it is rather than dressed up. */
const LOCALE_SOURCE: Record<string, string> = {
  field: "set on the field",
  manifest: "set on the template",
  project: "set on this project",
  region: "from this project's region",
  default: "a built-in default, not a choice anyone made",
};

const GATE_LEAF = "absolute inset-y-0 w-1/2";
const SLOT_BASE = "relative overflow-hidden rounded-lg border p-2.5";

type Row = {
  row_index: number;
  status: string;
  qa_passed?: boolean;
  qa_notes?: string[];
  open_tasks?: number;
  error?: string | null;
  is_canary?: boolean;
  document_version_id?: string | null;
};

/** Mirrors the runner's own test for a sample that closes the gate: a row that
 *  never rendered, or one that rendered and failed QA. Kept as one function so
 *  the panel cannot drift from the rule the server actually applied. */
function failsGate(row: Row) {
  return row.status === "failed" || row.qa_passed === false;
}

function isFailure(row: Row) {
  return row.status === "failed" || row.status === "blocked";
}

/* --------------------------------------------------------------------------
   Pieces
   -------------------------------------------------------------------------- */

/**
 * The QA engine's own sentences, all of them.
 *
 * These are the one genuinely per-check thing in the payload: "Condition 'c1'
 * was undecided" is specific, was written about this document, and is the only
 * place the reader can learn what the gate objected to. The old panel rendered
 * `qa_notes[0]` and dropped the rest into a `title` attribute, which is where
 * text goes to die.
 *
 * `still` is handed down rather than read here. Everything in this file below
 * the panel renders once per row of a batch that can be five thousand long, and
 * `useReducedMotionFlag()` opens a `matchMedia` and subscribes to it on every
 * call -- so a per-row hook is one media-query object and one listener per row,
 * torn down correctly and still O(rows) while the list is up. The panel reads
 * the flag once. `still` also carries the other reason to skip an entrance: a
 * list somebody expanded to five thousand rows wants to be on screen, not
 * staggered in.
 */
function QaNotes({ notes, dot, still }: { notes: string[]; dot: string; still: boolean }) {
  if (!notes.length) return null;
  return (
    <ul className="mt-1.5 space-y-1">
      {qaNoteLines(notes).map((note, i) => {
        const body = (
          <>
            <span aria-hidden className={cn("mt-[6px] h-1 w-1 shrink-0 rounded-full", dot)} />
            {/* Customer wording; the stored note stays verbatim for the audit. */}
            <span>{note}</span>
          </>
        );
        const line = "flex items-start gap-1.5 text-[11.5px] leading-relaxed text-muted-foreground";
        return still ? (
          <li key={`${i}-${note}`} className={line}>{body}</li>
        ) : (
          <motion.li
            key={`${i}-${note}`}
            initial={{ opacity: 0, x: -5 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: DUR.base, ease: EASE_OUT, delay: staggerDelay(i) }}
            className={line}
          >
            {body}
          </motion.li>
        );
      })}
    </ul>
  );
}

/** The 2px rule down the left of a failure, drawn rather than switched on.
 *  `scaleY` from a top origin, because animating height would lay the row out
 *  again on every frame.
 *
 *  `still` from the caller for the same reason as `QaNotes`: this is one per
 *  failed row. When it is set the rule is a plain span already at full height --
 *  no motion node, nothing to animate. */
function DrawnEdge({ tone = "bg-ai-blocked", still }: { tone?: string; still: boolean }) {
  const rule = cn("absolute left-0 top-0 h-full w-0.5 rounded-full", tone);
  if (still) return <span aria-hidden className={rule} />;
  return (
    <motion.span
      aria-hidden
      className={rule}
      style={{ transformOrigin: "top" }}
      initial={{ scaleY: 0 }}
      animate={{ scaleY: 1 }}
      transition={{ duration: DUR.reveal, ease: EASE_OUT }}
    />
  );
}

/** One sample slot. Either the verdict it arrived with, or a placeholder saying
 *  plainly that nothing has been reported for it yet -- never a made-up
 *  intermediate phase, because the runner does not publish one. */
function SampleSlot({ row, showNotes, still }: {
  row: Row | undefined; showNotes: boolean; still: boolean;
}) {
  if (!row) {
    return (
      <div className={cn(SLOT_BASE, "border-dashed border-border/70 bg-surface-elevated/20")}>
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Checked first</div>
        <SkeletonBar className="mt-2 h-2.5 w-20" />
        <div className="mt-2 text-[11px] text-muted-foreground">Not reported yet</div>
      </div>
    );
  }

  const meta = ROW_STATE[row.status] ?? UNKNOWN_ROW_STATE;
  const failed = isFailure(row);

  return (
    <motion.div
      initial={still ? false : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: DUR.reveal, ease: EASE_OUT }}
      className={cn(SLOT_BASE, failed
        ? "border-ai-blocked/35 bg-ai-blocked/[0.06]"
        : "border-border/70 bg-surface-elevated/30")}
    >
      {failed && <DrawnEdge still={still} />}
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
        Checked first · row {row.row_index}
      </div>
      <div className={cn("mt-1.5 inline-flex rounded-full border px-1.5 py-px text-[10.5px] font-medium", meta.chip)}>
        {meta.label}
      </div>
      {row.error && (
        <p className="mt-1.5 text-[11.5px] leading-relaxed text-ai-blocked">{plainly(String(row.error))}</p>
      )}
      {showNotes && <QaNotes notes={row.qa_notes ?? []} dot="bg-ai-uncertain/70" still={still} />}
    </motion.div>
  );
}

type GateState = "waiting" | "holding" | "open" | "shut" | "unknown";

/**
 * The gate itself.
 *
 * Two leaves that part when the samples pass, and do not when they fail. This is
 * the one moment in the run where a real decision is taken -- the runner
 * genuinely refuses to attempt the remaining rows unless every sample came back
 * clean -- so it is the one moment that gets a piece of machinery rather than a
 * status word.
 *
 * Nothing here moves on a timer. `state` is a function of what the runner
 * published, and the leaves are only allowed open once there is affirmative
 * evidence the batch went through: a row that is not a sample, or a job that
 * reached a finished status. Opening them the instant the last sample landed
 * would mean opening them for the fraction of a second before a `blocked`
 * verdict arrives, which is the one lie this graphic exists to not tell.
 */
function CanaryGate({ state, behind, reduced }: {
  state: GateState; behind: number | null; reduced: boolean;
}) {
  const open = state === "open";
  const shut = state === "shut";

  const leafTone = shut
    ? "border-ai-blocked/45 bg-ai-blocked/[0.12]"
    : state === "unknown"
      ? "border-border bg-surface-elevated/60"
      : "border-border-strong/70 bg-surface-elevated/70";

  const caption =
    state === "shut"
      ? behind && behind > 0
        ? "The rest of the batch was not attempted."
        : "Nothing further was attempted."
      : state === "open"
        ? behind && behind > 0
          ? "Passed — the rest of the batch continued."
          : "Passed — every document in this batch was checked first."
        : state === "unknown"
          ? "The run stopped before the first checks finished."
          : "The rest of the batch continues once these pass.";

  return (
    <div className="mt-3">
      <div className="relative h-12 overflow-hidden rounded-lg border border-border/70 bg-background/50">
        {/* What is behind the gate, revealed by the leaves rather than faded in:
            the text is always there and the doors are what move. */}
        <div className="absolute inset-0 flex items-center justify-center px-3 text-center text-[11.5px] leading-snug text-muted-foreground">
          {caption}
        </div>

        <motion.span
          aria-hidden
          className={cn(GATE_LEAF, "left-0 border-r", leafTone)}
          initial={reduced ? false : { x: "0%" }}
          animate={{ x: open ? "-100.5%" : "0%" }}
          transition={reduced ? { duration: 0 } : { duration: DUR.revealSlow, ease: EASE_OUT }}
        />
        <motion.span
          aria-hidden
          className={cn(GATE_LEAF, "right-0 border-l", leafTone)}
          initial={reduced ? false : { x: "0%" }}
          animate={{ x: open ? "100.5%" : "0%" }}
          transition={reduced ? { duration: 0 } : { duration: DUR.revealSlow, ease: EASE_OUT }}
        />

        {/* The seam. A padlock while the gate holds a batch back, and it engages
            with a single pop the moment the verdict is `blocked`. */}
        <motion.span
          aria-hidden
          className={cn(
            "absolute left-1/2 top-1/2 flex h-6 w-6 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border",
            shut
              ? "border-ai-blocked/50 bg-ai-blocked/15 text-ai-blocked"
              : "border-border-strong/70 bg-surface text-muted-foreground",
          )}
          initial={false}
          animate={{ opacity: open ? 0 : 1, scale: open ? 0.6 : 1 }}
          transition={reduced ? { duration: 0 } : SPRING_UI}
        >
          <motion.span
            key={shut ? "shut" : "hold"}
            initial={reduced || !shut ? false : { scale: 0.55, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            transition={reduced ? { duration: 0 } : { duration: 0.25, ease: EASE_OUT }}
          >
            <Lock className="h-3 w-3" />
          </motion.span>
        </motion.span>
      </div>
    </div>
  );
}

/** One count from the run, animated up to the figure the runner published.
 *  Only ever mounted once the job is finished, so the number it climbs to is the
 *  final one and never re-runs from zero on the next poll. */
function CountTile({ label, value, tone }: { label: string; value: number; tone: string }) {
  const shown = useCountUp(value, 700, true);
  return (
    <div className="rounded-lg border border-border/70 bg-surface-elevated/30 px-2.5 py-2">
      <div className={cn("font-mono text-[15px] font-semibold tabular-nums leading-none", tone)}>
        {Math.round(shown)}
      </div>
      <div className="mt-1 text-[10.5px] leading-tight text-muted-foreground">{label}</div>
    </div>
  );
}

/** One row of the stream. Terminal by construction -- the runner writes a row
 *  into `progress` only once it is finished -- so this shows a verdict and the
 *  engine's notes, and never a phase.
 *
 *  `still` is the whole entrance, on or off, and it arrives as a prop. This is
 *  the file's hottest component -- one instance per row of the batch -- so it
 *  neither reads `prefers-reduced-motion` for itself nor mounts a motion node
 *  when there is nothing to animate. */
function RowLine({ row, still }: { row: Row; still: boolean }) {
  const meta = ROW_STATE[row.status] ?? UNKNOWN_ROW_STATE;
  const failed = isFailure(row);
  const shell = cn("relative rounded-lg py-1.5 pl-3 pr-2", failed && "bg-ai-blocked/[0.05]");

  const body = (
    <>
      {failed && <DrawnEdge still={still} />}
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
          row {row.row_index}
        </span>
        <span className={cn("rounded-full border px-1.5 py-px text-[10.5px] font-medium", meta.chip)}>
          {meta.label}
        </span>
        {(row.open_tasks ?? 0) > 0 && (
          <span className="text-[11px] text-muted-foreground">
            {row.open_tasks} open question{row.open_tasks === 1 ? "" : "s"}
          </span>
        )}
      </div>

      {/* A row that *failed* carries `error`, not `qa_notes` -- the two are
          different outcomes. `qa_notes` is the QA gate rejecting a document that
          was produced; `error` is the engine never getting that far. Rendering
          only the notes meant every failure showed as a bare "row 0 · failed"
          with the reason sitting unread. */}
      {row.error && (
        <p className="mt-1 text-[11.5px] leading-relaxed text-ai-blocked">{plainly(String(row.error))}</p>
      )}
      <QaNotes
        notes={row.qa_notes ?? []}
        dot={failed ? "bg-ai-blocked/70" : "bg-ai-uncertain/70"}
        still={still}
      />
    </>
  );

  if (still) return <li className={shell}>{body}</li>;

  return (
    <motion.li
      initial={{ opacity: 0, x: -6 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: DUR.base, ease: EASE_OUT }}
      className={shell}
    >
      {body}
    </motion.li>
  );
}

/* --------------------------------------------------------------------------
   The panel
   -------------------------------------------------------------------------- */

export function BatchProgressPanel({ watch, className }: { watch: BatchWatch; className?: string }) {
  const [showAllRows, setShowAllRows] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const reduced = useReducedMotionFlag();
  const visible = usePageVisible();
  const { job } = watch;

  const progress = job?.progress ?? {};
  // Memoised on the published array rather than re-derived: the fallback is a
  // fresh `[]` on every render, which would give the three lists below a new
  // identity on every poll for a job that has not reported a row yet.
  const rows: Row[] = useMemo(() => progress.rows ?? [], [progress.rows]);

  const samples = useMemo(() => rows.filter((r) => r.is_canary), [rows]);
  const stream = useMemo(() => rows.filter((r) => !r.is_canary), [rows]);
  const failedSamples = useMemo(() => samples.filter(failsGate), [samples]);

  // The collapsed list spends almost all of its budget on failures, because a
  // row that failed is the only reason anybody scrolls it -- successes get
  // ROWS_SHOWN, failures get FAILURES_SHOWN, which is four times as many.
  //
  // Failures are capped now, where before they were exempt. A batch is one
  // document per source row, so a bad mapping that failed every row mounted one
  // animated list item per failure -- five thousand of them, of which nobody
  // reads the fifth. What is over the cap is counted and stated below rather
  // than dropped silently, and "Show all" still renders every one.
  //
  // Computed above the early return, with every other hook, because `job` goes
  // from null to present the moment a batch starts and a hook that lives on the
  // far side of that return would change the hook count mid-life.
  const { shownStream, hiddenFailures } = useMemo(() => {
    if (showAllRows) return { shownStream: stream, hiddenFailures: 0 };
    let rowBudget = ROWS_SHOWN;
    let failBudget = FAILURES_SHOWN;
    let dropped = 0;
    const kept: Row[] = [];
    for (const r of stream) {
      if (isFailure(r)) {
        if (failBudget > 0) { failBudget -= 1; kept.push(r); }
        else dropped += 1;
      } else if (rowBudget > 0) {
        rowBudget -= 1;
        kept.push(r);
      }
    }
    return { shownStream: kept, hiddenFailures: dropped };
  }, [stream, showAllRows]);

  if (!job) return null;

  const outcome = OUTCOME[job.status] ?? OUTCOME.running;
  const terminal = TERMINAL_JOB_STATES.includes(job.status);
  const finishedClean = job.status === "completed" || job.status === "completed_with_errors";

  // `canary_size` is written by the runner's first publish, which happens once
  // the first sample has been rendered and checked. Until then there is no
  // honest number of slots to draw, so the gate section is simply not there --
  // hard-coding three would be this file inventing the batch's own shape.
  const sampleSize: number | null =
    typeof progress.canary_size === "number" ? progress.canary_size : null;
  const rowsTotal: number | null =
    typeof progress.rows_total === "number" && progress.rows_total > 0 ? progress.rows_total : null;
  const rowsDone: number = typeof progress.rows_done === "number" ? progress.rows_done : 0;
  const behind = sampleSize != null && rowsTotal != null ? rowsTotal - sampleSize : null;

  // The gate is only drawn open on evidence that the batch went through it: a
  // row that was not a sample, or a finished job. See `CanaryGate`.
  const samplesIn = sampleSize != null && samples.length >= sampleSize;
  const gate: GateState =
    sampleSize == null ? "waiting"
      : job.status === "blocked" ? "shut"
      // A row that is not a sample is proof the gate opened, whatever happened
      // to the run afterwards -- a batch that threw on row 900 still went
      // through it, and reading that as "no verdict" would hide 899 rows.
      : stream.length > 0 ? "open"
      : job.status === "failed" ? (samplesIn ? "unknown" : "waiting")
      : samplesIn && finishedClean ? "open"
      : samplesIn ? "holding"
      : "waiting";

  const ratio = rowsTotal ? Math.min(1, rowsDone / rowsTotal) : 0;

  const download = async () => {
    setDownloading(true);
    try {
      const url = await api.batchZipUrl(job.job_id);
      const a = document.createElement("a");
      a.href = url;
      a.download = "batch.zip";
      a.click();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      toast.error("Could not download this batch", { description: plainly(String(e?.message ?? e)) });
    } finally {
      setDownloading(false);
    }
  };

  return (
    <motion.section
      initial={reduced ? false : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: DUR.reveal, ease: EASE_OUT }}
      className={cn(
        "relative overflow-hidden rounded-2xl border bg-surface-elevated/40 p-4 sm:p-5",
        outcome.edge,
        watch.running && "glow-ai",
        className,
      )}
    >
      {/* The scanner: one thin line travelling down the panel while work is in
          flight and gone the instant it is not, so "still moving" is a property
          of the animation rather than something the reader has to infer from a
          number that has not changed in forty seconds. Paused with the tab,
          because a composited loop nobody can see is battery for nothing. */}
      {watch.running && !reduced && visible && (
        <span aria-hidden className="pointer-events-none absolute inset-x-0 top-0 h-full overflow-hidden">
          <span className="scan-sweep absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-ai-active to-transparent opacity-70" />
        </span>
      )}
      <span aria-hidden className="grid-noise pointer-events-none absolute inset-0 opacity-[0.3]" />

      <div className="relative space-y-4">
        {/* ---- header ---- */}
        <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2" aria-live="polite">
              {watch.running && <Loader2 className="h-4 w-4 shrink-0 animate-spin text-ai-active" />}
              <span className={cn("text-[14px] font-semibold tracking-tight", outcome.tone)}>
                {outcome.heading}
              </span>
            </div>
            <p className="mt-1 text-[12.5px] leading-relaxed text-muted-foreground">{outcome.hint}</p>
          </div>

          {rowsTotal != null && (
            <div className="shrink-0 text-right">
              <div className="font-mono text-[13px] tabular-nums text-foreground">
                {rowsDone.toLocaleString()}/{rowsTotal.toLocaleString()}
              </div>
              <div className="mt-0.5 text-[10.5px] uppercase tracking-wide text-muted-foreground">
                rows reported
              </div>
            </div>
          )}
        </div>

        {/* ---- progress ---- */}
        {rowsTotal != null && (
          <div className="h-1.5 overflow-hidden rounded-full bg-border/60">
            <motion.div
              aria-hidden
              className={cn("h-full w-full rounded-full", outcome.bar)}
              style={{ transformOrigin: "left" }}
              initial={reduced ? false : { scaleX: 0 }}
              animate={{ scaleX: ratio }}
              transition={reduced ? { duration: 0 } : SPRING_PROGRESS}
            />
          </div>
        )}

        {/* The reason, on every state that has one except `blocked` -- that one
            is explained at the gate, where the reader is already looking.
            This used to render `job.error` for `blocked` only, so a batch that
            *failed* showed the bare word "failed" and the traceback the server
            had gone to the trouble of recording sat unread in the payload. */}
        {job.error && job.status !== "blocked" && (
          <div className="rounded-lg border border-ai-blocked/35 bg-ai-blocked/[0.08] p-2.5 text-[12px] leading-relaxed text-foreground">
            {plainly(String(job.error))}
          </div>
        )}

        {watch.error && (
          <div className="flex flex-wrap items-center gap-2 text-[11.5px] text-ai-uncertain">
            <span className="inline-flex items-start gap-1.5">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {watch.stopped
                ? `Stopped watching this batch: ${watch.error} It may still have finished.`
                : watch.error}
            </span>
            {watch.stopped && (
              <Button size="sm" variant="outline" onClick={watch.resume}>
                <RefreshCw className="h-3.5 w-3.5" /> Check again
              </Button>
            )}
          </div>
        )}

        {/* ---- the gate ---- */}
        {sampleSize != null && (
          <section
            className={cn(
              "rounded-xl border p-3",
              gate === "shut"
                ? "border-ai-blocked/40 bg-ai-blocked/[0.05]"
                : "border-border/70 bg-background/30",
            )}
          >
            <div className="flex items-start gap-2">
              <ShieldCheck
                className={cn("mt-px h-4 w-4 shrink-0", gate === "shut" ? "text-ai-blocked" : "text-ai-active")}
              />
              <div className="min-w-0">
                <h4 className="text-[12.5px] font-semibold tracking-tight">Checked first</h4>
                <p className="mt-0.5 text-[11.5px] leading-relaxed text-muted-foreground">
                  A few documents are checked first; the rest continue once they pass.
                </p>
              </div>
            </div>

            <div
              className="mt-3 grid gap-2"
              style={{ gridTemplateColumns: `repeat(auto-fit, minmax(9.5rem, 1fr))` }}
            >
              {/* Arrived slots plus one pending placeholder, so the screen
                  never draws how many documents are checked first. */}
              {samples.slice(0, sampleSize).map((row, i) => (
                <SampleSlot key={i} row={row} showNotes={gate !== "shut"} still={reduced} />
              ))}
              {samples.length < sampleSize && (
                <SampleSlot row={undefined} showNotes={false} still={reduced} />
              )}
            </div>

            <CanaryGate state={gate} behind={behind} reduced={reduced} />

            {/* Shut: the gate is the centre of the panel and this is what it
                found, in the QA engine's own sentences and all of them. */}
            {gate === "shut" && (
              <motion.div
                initial={reduced ? false : { opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: DUR.reveal, ease: EASE_OUT, delay: reduced ? 0 : DUR.micro }}
                className="mt-3 rounded-lg border border-ai-blocked/35 bg-ai-blocked/[0.07] p-3"
              >
                <p className="text-[12px] font-medium leading-relaxed text-foreground">
                  {job.error ? plainly(String(job.error)) : "The first documents checked did not pass, so the batch stopped here."}
                </p>
                <ul className="mt-2.5 space-y-2.5">
                  {failedSamples.map((row) => (
                    <li key={row.row_index} className="relative pl-3">
                      <DrawnEdge still={reduced} />
                      <div className="text-[11.5px] font-medium text-foreground">
                        Row {row.row_index} · {(ROW_STATE[row.status] ?? UNKNOWN_ROW_STATE).label}
                      </div>
                      {row.error && (
                        <p className="mt-1 text-[11.5px] leading-relaxed text-ai-blocked">
                          {plainly(String(row.error))}
                        </p>
                      )}
                      {(row.qa_notes ?? []).length > 0 ? (
                        <QaNotes notes={row.qa_notes ?? []} dot="bg-ai-blocked/70" still={reduced} />
                      ) : !row.error ? (
                        /* The null branch says so rather than showing an empty
                           list, which would read as "nothing was wrong". */
                        <p className="mt-1 text-[11.5px] leading-relaxed text-muted-foreground">
                          No note was recorded against this row.
                        </p>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </motion.div>
            )}
          </section>
        )}

        {/* ---- the stream ----
            Held back until the gate has visibly opened. The rows genuinely do
            not exist until then, so this is ordering the reveal rather than
            delaying information. */}
        {stream.length > 0 && gate !== "shut" && (
          <motion.div
            initial={reduced ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: DUR.base, ease: EASE_OUT, delay: reduced ? 0 : DUR.revealSlow }}
            className="space-y-1.5"
          >
            <h4 className="text-[10.5px] font-medium uppercase tracking-wide text-muted-foreground">
              The rest of the batch
            </h4>
            <ul className="space-y-0.5">
              {showAllRows ? (
                /* Somebody who pressed "Show all" on a five-thousand-row batch
                   asked for the rows, not for a five-thousand-step entrance. The
                   expanded list is plain markup: no motion node per row, and
                   nothing for AnimatePresence to keep track of. */
                shownStream.map((row) => <RowLine key={row.row_index} row={row} still />)
              ) : (
                /* `initial={false}`: a job that was already finished when this
                   mounted arrives as one block rather than replaying every
                   entrance, and rows that land on a later poll animate in as
                   they arrive. */
                <AnimatePresence initial={false}>
                  {shownStream.map((row) => (
                    <RowLine key={row.row_index} row={row} still={reduced} />
                  ))}
                </AnimatePresence>
              )}
            </ul>
            {hiddenFailures > 0 && (
              /* Stated, not swallowed. The collapsed list caps what it draws, and
                 a reader who is told a row failed but cannot find it has no way
                 to tell a cap from a clean batch. */
              <p className="text-[11px] leading-relaxed text-ai-blocked">
                …and {hiddenFailures.toLocaleString()} more failed row
                {hiddenFailures === 1 ? "" : "s"} not listed here.
              </p>
            )}
            {(showAllRows || shownStream.length < stream.length) && (
              <button
                onClick={() => setShowAllRows((v) => !v)}
                className="text-[11px] text-muted-foreground underline decoration-dotted underline-offset-2 transition-colors hover:text-foreground"
              >
                {showAllRows ? "Show fewer" : `Show all ${stream.length} rows`}
              </button>
            )}
          </motion.div>
        )}

        {/* ---- what the run came to ---- */}
        {terminal && (
          rows.length > 0 ? (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <CountTile label="Passed checks" value={progress.generated ?? 0} tone="text-ai-confident" />
              <CountTile label="Need a decision" value={progress.pending_review ?? 0} tone="text-ai-uncertain" />
              <CountTile label="Failed checks" value={progress.blocked ?? 0} tone="text-ai-blocked" />
              <CountTile label="Not produced" value={progress.failed ?? 0} tone="text-ai-blocked" />
            </div>
          ) : (
            /* No counts because no row was ever reported -- said out loud, so
               four zeroes cannot read as "we counted, and it was none". */
            <p className="text-[11.5px] leading-relaxed text-muted-foreground">
              This run stopped before it reported any rows, so there is nothing to count.
            </p>
          )
        )}

        {/* ---- the archive ---- */}
        {finishedClean && (
          <div className="flex flex-wrap items-center gap-2 border-t border-border/60 pt-3">
            <Button size="sm" variant="outline" onClick={download} disabled={downloading}>
              {downloading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
              Download the approved ones
            </Button>
            <span className="text-[11.5px] text-muted-foreground">
              A batch archive carries only documents that have been approved; the rest are named in
              a note inside it.
            </span>
          </div>
        )}

        {/* ---- footnotes ----
            Product copy, deliberately not a metric chip: nothing in this payload
            asserts a model call count per run, and a badge would read as one. The
            fill path is rules on the server, which is why a batch moves at this
            speed, and that is worth saying once, quietly. */}
        <div className="space-y-1 border-t border-border/60 pt-3 text-[11px] leading-relaxed text-muted-foreground">
          <p>Every document is filled from your data and checked before it's ready.</p>
          {progress.locale && (
            <p>
              Dates and numbers formatted for{" "}
              <span className="font-mono text-foreground/80">{String(progress.locale).replace("_", "-")}</span>
              {LOCALE_SOURCE[progress.locale_source]
                ? ` — ${LOCALE_SOURCE[progress.locale_source]}.`
                : "."}
            </p>
          )}
        </div>
      </div>
    </motion.section>
  );
}
