import { useCallback, useEffect, useRef, useState } from "react";
import { Brain, CheckCircle2, Cpu, Database, Loader2, Search, XCircle } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * What the compiler is doing, while it is doing it.
 *
 * Compiling is between two seconds and three minutes, and from the outside both
 * look like the same spinner. The distinction worth showing is not how far along
 * it is but what sort of work is running: a template read by the colour rules
 * costs nothing and finishes instantly, while one handed to a model costs real
 * money and takes minutes. A reviewer watching a spinner cannot tell those
 * apart, and cannot tell either from a request that has hung.
 */

export type StageKind = "deterministic" | "retrieval" | "model" | "embedding";

export interface Stage {
  key: string;
  label: string;
  kind: StageKind;
  status: "running" | "done" | "failed";
  detail?: string | null;
}

const KIND_STYLE: Record<StageKind, { icon: typeof Cpu; tint: string; word: string }> = {
  deterministic: { icon: Cpu, tint: "text-sky-500", word: "on this machine" },
  retrieval: { icon: Search, tint: "text-violet-500", word: "searching your data" },
  model: { icon: Brain, tint: "text-amber-500", word: "AI model" },
  embedding: { icon: Database, tint: "text-emerald-500", word: "building the index" },
};

/**
 * Polls one compile's progress. Returns a token to hand to `compileManifest`.
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
        if (job?.status === "failed") setFailed(job?.error ?? "The compile failed.");
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

export function CompileProgressList({ stages, failed }: { stages: Stage[]; failed?: string | null }) {
  if (!stages.length && !failed) return null;
  return (
    <div className="rounded-lg border border-border bg-background/60 p-3 space-y-2">
      {stages.map((s) => {
        const style = KIND_STYLE[s.kind] ?? KIND_STYLE.deterministic;
        const Icon = style.icon;
        return (
          <div key={s.key} className="flex items-start gap-2.5 text-xs">
            <span className={cn("mt-0.5 shrink-0", style.tint)}>
              {s.status === "running" ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : s.status === "failed" ? (
                <XCircle className="h-3.5 w-3.5 text-destructive" />
              ) : (
                <CheckCircle2 className="h-3.5 w-3.5" />
              )}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2">
                <span className={cn("font-medium", s.status === "done" ? "text-muted-foreground" : "text-foreground")}>
                  {s.label}
                </span>
                {/* Naming the kind is the point: "AI model" is the stage that
                    costs money and minutes, and it should be obvious which one
                    the compile is sitting in. */}
                <span className={cn("inline-flex items-center gap-1 text-[10px] uppercase tracking-wide", style.tint)}>
                  <Icon className="h-3 w-3" /> {style.word}
                </span>
              </div>
              {s.detail && <div className="text-muted-foreground mt-0.5">{s.detail}</div>}
            </div>
          </div>
        );
      })}
      {failed && <div className="text-xs text-destructive">{failed}</div>}
    </div>
  );
}
