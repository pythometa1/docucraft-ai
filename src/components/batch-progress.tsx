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
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, Download, Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
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
          const reason = e?.message ?? String(e);
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

/** How each terminal state should read. Separated from the markup because the
 *  distinction the old version missed was exactly here: `failed` and `blocked`
 *  are different things and only one of them was being explained. */
const OUTCOME: Record<string, { tone: string; heading: string; hint: string }> = {
  queued: { tone: "text-muted-foreground", heading: "Queued", hint: "Waiting for a worker to pick this up." },
  running: { tone: "text-brand", heading: "Generating", hint: "Documents are being filled and checked." },
  completed: { tone: "text-emerald-500", heading: "Finished", hint: "Every row produced a document." },
  completed_with_errors: {
    tone: "text-amber-500", heading: "Finished, with failures",
    hint: "Some rows did not produce a document. The ones that did are in the project.",
  },
  blocked: {
    tone: "text-destructive", heading: "Stopped before it ran",
    hint: "The first few documents failed their checks, so the rest were never attempted — "
      + "that gate exists so a bad mapping costs three documents rather than a thousand.",
  },
  failed: {
    tone: "text-destructive", heading: "Failed",
    hint: "The batch stopped on an error rather than a QA verdict. Nothing further was attempted.",
  },
};

export function BatchProgressPanel({ watch, className }: { watch: BatchWatch; className?: string }) {
  const [showAllRows, setShowAllRows] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const { job } = watch;
  if (!job) return null;

  const progress = job.progress ?? {};
  const rows: any[] = progress.rows ?? [];
  const shown = showAllRows ? rows : rows.slice(0, ROWS_SHOWN);
  const outcome = OUTCOME[job.status] ?? OUTCOME.running;

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
      toast.error("Could not download this batch", { description: e?.message ?? String(e) });
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className={cn("rounded-xl border border-border bg-background/40 p-4 space-y-2.5", className)}>
      <div className="flex flex-wrap items-center gap-2">
        {watch.running && <Loader2 className="h-4 w-4 animate-spin text-brand" />}
        <span className={cn("text-sm font-medium", outcome.tone)}>{outcome.heading}</span>
        {progress.rows_total != null && (
          <span className="text-xs text-muted-foreground">
            {progress.rows_done ?? 0} of {progress.rows_total}
            {progress.blocked ? <span className="text-rose-500"> · {progress.blocked} blocked</span> : null}
          </span>
        )}
      </div>
      <p className="text-xs text-muted-foreground">{outcome.hint}</p>

      {/* The reason, on every state that has one.
          This used to render `job.error` for `blocked` only, so a batch that
          *failed* showed the bare word "failed" and the traceback the server had
          gone to the trouble of recording sat unread in the payload. */}
      {job.error && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-2.5 text-xs text-destructive">
          {job.error}
        </div>
      )}

      {watch.error && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-amber-500">
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

      {shown.map((r: any) => (
        <div key={r.row_index} className="flex items-center gap-2 text-[11px]">
          <span className={cn("rounded px-1.5 py-0.5",
            r.status === "generated" ? "bg-emerald-500/10 text-emerald-500"
              : r.status === "blocked" || r.status === "failed" ? "bg-rose-500/10 text-rose-500"
              : "bg-amber-500/10 text-amber-500")}>
            row {r.row_index} · {r.status}
          </span>
          {r.is_canary && <span className="text-muted-foreground">canary</span>}
          {(r.qa_notes ?? []).length > 0 && (
            <span className="truncate text-muted-foreground" title={r.qa_notes.join("; ")}>
              {r.qa_notes[0]}
            </span>
          )}
          {/* A row that *failed* carries `error`, not `qa_notes` -- the two are
              different outcomes. `qa_notes` is the QA gate rejecting a document
              that was produced; `error` is the engine never getting that far.
              Rendering only the notes meant every failure showed as a bare
              "row 0 · failed" with the reason sitting unread. */}
          {r.error && (
            <span className="truncate text-rose-400" title={r.error}>{r.error}</span>
          )}
        </div>
      ))}

      {rows.length > ROWS_SHOWN && (
        <button
          onClick={() => setShowAllRows((v) => !v)}
          className="text-[11px] text-muted-foreground underline decoration-dotted hover:text-foreground"
        >
          {showAllRows ? "Show fewer" : `Show all ${rows.length} rows`}
        </button>
      )}

      {["completed", "completed_with_errors"].includes(job.status) && (
        <div className="flex flex-wrap items-center gap-2 pt-1">
          <Button size="sm" variant="outline" onClick={download} disabled={downloading}>
            {downloading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
            Download the approved ones
          </Button>
          <span className="text-xs text-muted-foreground">
            A batch archive carries only documents that have been approved; the rest are named in
            a note inside it.
          </span>
        </div>
      )}
    </div>
  );
}
