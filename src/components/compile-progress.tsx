import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { CompileReveal } from "@/components/compile-reveal";
import { useBackgroundTasks } from "@/lib/background-tasks";

/**
 * The progress feed behind the processing banner.
 *
 * This module owns the polling and the stage shape; `CompileReveal` owns how it
 * looks. The three screens that mount it all render `CompileProgressList`, a
 * thin adaptor onto the reveal.
 *
 * The server reports three plain steps -- reading, understanding, finishing --
 * and nothing about how each is done. That is deliberate: the engine's own
 * stage list describes the pipeline, and stays on the server.
 */

export type StageKey = "read" | "analyse" | "finish";

export interface Stage {
  key: StageKey | string;
  label: string;
  status: "pending" | "running" | "done" | "failed";
  /** Seconds the step has taken so far, or took. Null before it starts. */
  elapsed_seconds?: number | null;
}

/**
 * Polls one run's progress. Returns a token to hand to the read request.
 *
 * Polling starts when `active` goes true and stops on a terminal status, so a
 * finished compile is not still being asked about a minute later.
 */
export function useCompileProgress(active: boolean) {
  const [stages, setStages] = useState<Stage[]>([]);
  const [failed, setFailed] = useState<string | null>(null);
  const tokenRef = useRef<string>("");

  const newToken = useCallback(() => {
    tokenRef.current = crypto.randomUUID();
    setStages([]);
    setFailed(null);
    return tokenRef.current;
  }, []);

  useEffect(() => {
    if (!active || !tokenRef.current) return;
    let live = true;
    const tick = async () => {
      try {
        const job: any = await api.getJob(tokenRef.current);
        if (!live) return;
        setStages(((job?.progress?.stages ?? []) as Stage[]).slice());
        if (job?.status === "failed") setFailed(job?.error ?? "Processing stopped before it finished.");
      } catch {
        // A 404 is the normal first second or two: the row is written by the
        // first stage, and the poll can beat it. Anything else is transient
        // enough that the next tick is a better answer than an error message.
      }
    };
    void tick();
    const id = setInterval(tick, 1200);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [active]);

  return { token: tokenRef.current, newToken, stages, failed };
}

/**
 * The same feed for a reading that runs in the background.
 *
 * `useCompileProgress` polls from the component, which is right for a request
 * the component itself is holding open. A background reading outlives any one
 * component -- the dialog closes, the route changes -- so its polling is owned
 * by `useBackgroundTasks`, and this is the read side of it.
 */
export function useReadingTask(token: string | null | undefined) {
  return useBackgroundTasks((s) => (token ? s.tasks[token] : undefined));
}

/**
 * Kept as the name every caller already imports. The rendering moved to
 * `CompileReveal`; this stays so upgrading the visuals did not mean touching the
 * three screens that show progress.
 *
 * `result` is the compile response body, and it is optional for a reason worth
 * stating: the progress feed and the response are two different arrivals. The
 * stages come off a poll, the body comes off the request that started it, and a
 * screen that never captured the second one is a screen that shows the stage
 * list and stops -- which is correct, and is what this looked like before. Where
 * a caller does hand it over, the panel can close the loop and report what was
 * actually found. Nothing here fabricates the difference.
 */
export function CompileProgressList({
  stages,
  failed,
  title,
  className,
  result,
}: {
  stages: Stage[];
  failed?: string | null;
  title?: string;
  className?: string;
  result?: any;
}) {
  return (
    <CompileReveal
      stages={stages}
      failed={failed}
      title={title}
      className={className}
      result={result}
    />
  );
}
