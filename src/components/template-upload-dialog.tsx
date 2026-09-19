import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useLocation } from "@tanstack/react-router";
import { AnimatePresence, motion } from "framer-motion";
import { Check, FileText, Loader2, Sparkles, UploadCloud, X, Zap } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { useStore } from "@/lib/store";
import { cn } from "@/lib/utils";
import { EASE_OUT, useReducedMotionFlag } from "@/components/motion";
import { plainly } from "@/components/processing-banner";
import { useReadingTask } from "@/components/compile-progress";
import {
  completedSteps,
  currentStepIndex,
  summaryLine,
  useBackgroundTasks,
  type ReadingTask,
} from "@/lib/background-tasks";
import type { ReadingStep } from "@/lib/types";

/**
 * Upload a template and watch it being read -- or send the reading to the
 * background and carry on.
 *
 * The reading runs on the server (`background=true`), so this dialog is only a
 * window onto it: the progress lives in `useBackgroundTasks`, which keeps
 * polling after "Run in background" closes this, and re-opening the dialog from
 * the tray, or from "Read again", attaches to the same task.
 *
 * Everything on screen is something the server reported: the three public
 * steps, the counts it has actually found, the result. The percentage is steps
 * done out of three, not a smoothed guess.
 */

const ACCEPT = ".docx,.dotx";

/** What each public step is doing, in the reader's words. */
const STEP_DESCRIPTION: Record<string, string> = {
  read: "Opening the document and finding its placeholders.",
  analyse: "Working out the values, instructions and optional sections.",
  finish: "Saving what was found so the template is ready to use.",
};

const DEFAULT_STEPS: ReadingStep[] = [
  { key: "read", label: "Reading your template", status: "running" },
  { key: "analyse", label: "Understanding the structure", status: "pending" },
  { key: "finish", label: "Finishing up", status: "pending" },
];

type Phase = "pick" | "ready" | "uploading";

export function formatBytes(bytes: number | null | undefined): string | null {
  if (bytes == null) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDuration(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds)) return null;
  if (seconds < 1) return "<1s";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

function extensionOf(name: string): string {
  const m = /\.([a-z0-9]+)$/i.exec(name);
  return m ? m[1].toUpperCase() : "DOCX";
}

/** Seconds since `from`, ticking once a second while `live`. */
function useSecondsSince(from: number | undefined, until: number | undefined, live: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!live) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [live]);
  if (from == null) return null;
  return Math.max(0, ((until ?? (live ? now : Date.now())) - from) / 1000);
}

export function TemplateUploadDialog({
  open,
  onOpenChange,
  projectId,
  attachToken,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** Where a new upload goes. Not needed when attaching to a running task. */
  projectId?: string;
  /** Attach to a reading already under way ("Read again", the tray). */
  attachToken?: string | null;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const reduced = useReducedMotionFlag();
  const loadProjectDetail = useStore((s) => s.loadProjectDetail);
  const track = useBackgroundTasks((s) => s.track);
  const setWatching = useBackgroundTasks((s) => s.setWatching);
  const setFocus = useBackgroundTasks((s) => s.setFocus);
  const dismiss = useBackgroundTasks((s) => s.dismiss);

  const [phase, setPhase] = useState<Phase>("pick");
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [token, setToken] = useState<string | null>(attachToken ?? null);
  // Set once the file is on the server, so a retry after the reading failed to
  // start reads that copy instead of uploading a second one.
  const [uploaded, setUploaded] = useState<{ id: string; name: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const task = useReadingTask(token);

  // A fresh open starts clean; an attached open shows its task.
  useEffect(() => {
    if (!open) return;
    setToken(attachToken ?? null);
    if (!attachToken) {
      setPhase("pick");
      setFile(null);
      setUploaded(null);
      setError(null);
    }
  }, [open, attachToken]);

  // Watched tasks finish on screen, so the store skips their toast.
  useEffect(() => {
    if (open && token) {
      setWatching(token);
      return () => setWatching(null);
    }
  }, [open, token, setWatching]);

  const running = task?.status === "running";
  const done = task?.status === "done";
  const failed = task?.status === "failed";

  const close = () => {
    if (phase === "uploading") return;
    // A finished task has been seen here; it need not wait in the tray too.
    if (token && task && task.status !== "running") dismiss(token);
    onOpenChange(false);
  };

  const choose = (files: FileList | null) => {
    const f = files?.[0];
    if (!f) return;
    // The same rule the server applies, said before the upload rather than after.
    if (!/\.(docx|dotx)$/i.test(f.name)) {
      setError("Only Word templates (.docx or .dotx) can be read.");
      return;
    }
    setError(null);
    setFile(f);
    setUploaded(null);
    setPhase("ready");
  };

  const start = async () => {
    if (!file || !projectId || phase === "uploading") return;
    setPhase("uploading");
    setError(null);
    let target = uploaded;
    if (!target) {
      try {
        const res = await api.uploadTemplate(projectId, file, file.name);
        target = { id: res.id, name: res.name ?? file.name };
        setUploaded(target);
      } catch (e: any) {
        setPhase("ready");
        setError(plainly(String(e?.message ?? e)));
        return;
      }
    }
    // Minted here, before the request, so the row the server writes and the
    // task this browser tracks are the same one from the first poll.
    const progressToken = crypto.randomUUID();
    try {
      await api.compileManifestBackground(target.id, progressToken);
      track({
        token: progressToken, templateName: target.name, projectId,
        templateId: target.id, sizeBytes: file.size, startedAt: Date.now(),
      });
      setToken(progressToken);
    } catch (e: any) {
      setError(
        `${plainly(String(e?.message ?? e))} The file itself was uploaded — use “Read again” on its row to try once more.`,
      );
    } finally {
      setPhase("ready");
      void loadProjectDetail(projectId).catch(() => undefined);
    }
  };

  const viewTemplate = () => {
    if (!task) return close();
    setFocus({ projectId: task.projectId, templateId: task.templateId });
    close();
    if (!location.pathname.startsWith(`/projects/${task.projectId}`)) {
      void navigate({ to: "/projects/$id", params: { id: task.projectId } });
    }
  };

  const stages = task?.stages?.length ? task.stages : task ? DEFAULT_STEPS : [];
  const stepsDone = task ? completedSteps({ status: task.status, stages }) : 0;
  const percent = Math.round((100 * stepsDone) / 3);
  const elapsed = useSecondsSince(task?.startedAt, task?.finishedAt, running);

  const fileName = task?.templateName ?? file?.name ?? "";
  const sizeBytes = task?.sizeBytes ?? file?.size;
  const fileState = task
    ? running ? "Reading…" : done ? "Read" : "Could not be read"
    : phase === "uploading" ? "Uploading…" : uploaded ? "Uploaded" : "Ready to read";

  const enter = reduced
    ? { initial: false as const, animate: { opacity: 1 } }
    : {
        initial: { opacity: 0, y: 6 },
        animate: { opacity: 1, y: 0 },
        exit: { opacity: 0, y: -4 },
        transition: { duration: 0.22, ease: EASE_OUT },
      };

  return (
    <Dialog open={open} onOpenChange={(v) => (v ? onOpenChange(true) : close())}>
      <DialogContent
        className={cn(
          // One shrinkable column: a grid item defaults to its content's width,
          // and a long file name would otherwise push the dialog past its edge.
          "max-w-[560px] grid-cols-[minmax(0,1fr)] gap-0 overflow-hidden rounded-2xl border-border-strong/70 bg-surface p-0",
          "shadow-[0_24px_80px_-24px_oklch(0_0_0/0.6)] sm:rounded-2xl",
          // The built-in close sits in the header's corner, clear of the tile.
          "[&>button:last-child]:right-5 [&>button:last-child]:top-5",
        )}
        onInteractOutside={(e) => { if (phase === "uploading") e.preventDefault(); }}
        // No focus ring on the close button the moment it opens; focus stays
        // trapped inside and Tab reaches every control.
        onOpenAutoFocus={(e) => e.preventDefault()}
      >
        {/* Header */}
        <div className="flex items-start gap-3.5 px-6 pb-5 pt-6 pr-12">
          <div className="relative grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-brand/30 bg-brand/10 text-brand">
            <Sparkles className="h-[18px] w-[18px]" />
          </div>
          <div className="min-w-0">
            <DialogTitle className="text-[17px] font-semibold leading-6 tracking-tight">Upload template</DialogTitle>
            <DialogDescription className="mt-0.5 text-[13px] leading-5 text-muted-foreground">
              DocuMind AI reads placeholders, author instructions and conditional sections.
            </DialogDescription>
          </div>
        </div>

        <div className="space-y-3 px-6">
          {/* File card, or the drop zone before a file is chosen */}
          {fileName ? (
            <div className="flex items-center gap-3 rounded-xl border border-border bg-background/40 p-3 light:bg-muted/40">
              <div className="grid h-10 w-10 shrink-0 place-items-center rounded-lg border border-border bg-surface-elevated text-info">
                <FileText className="h-[18px] w-[18px]" />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="truncate font-mono text-[13px] font-medium" title={fileName}>{fileName}</span>
                  <span className="shrink-0 rounded border border-border px-1.5 py-px font-mono text-[9.5px] font-semibold tracking-wider text-muted-foreground">
                    {extensionOf(fileName)}
                  </span>
                </div>
                <div className="mt-0.5 text-xs text-muted-foreground">
                  {[formatBytes(sizeBytes), fileState].filter(Boolean).join(" · ")}
                </div>
              </div>
              {/* Only for a file not yet sent. Once reading starts it is the
                  server's copy being read, and swapping it would mean a new one. */}
              {!attachToken && (
                <button
                  type="button"
                  onClick={() => inputRef.current?.click()}
                  disabled={!!task || !!uploaded || phase === "uploading"}
                  className="shrink-0 rounded-md px-2 py-1 text-xs font-medium text-brand hover:bg-brand/10 disabled:pointer-events-none disabled:opacity-40"
                >
                  Replace
                </button>
              )}
            </div>
          ) : (
            <label
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => { e.preventDefault(); setDragging(false); choose(e.dataTransfer.files); }}
              className={cn(
                "flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-10 text-center transition-colors",
                dragging ? "border-brand bg-brand/5" : "border-border hover:border-border-strong hover:bg-accent/30",
              )}
            >
              <div className="mb-3 grid h-11 w-11 place-items-center rounded-xl border border-border bg-surface-elevated text-muted-foreground">
                <UploadCloud className="h-5 w-5" />
              </div>
              <div className="text-sm font-medium">
                Drop a .docx here or <span className="text-brand underline-offset-2 hover:underline">browse</span>
              </div>
              <div className="mt-1 text-xs text-muted-foreground">Word templates, .docx or .dotx</div>
              <input type="file" accept={ACCEPT} className="hidden" onChange={(e) => { choose(e.target.files); e.target.value = ""; }} />
            </label>
          )}
          <input ref={inputRef} type="file" accept={ACCEPT} className="hidden"
                 onChange={(e) => { choose(e.target.files); e.target.value = ""; }} />

          <AnimatePresence initial={false}>
            {task && (
              <motion.div key="processing" {...enter} className="space-y-3">
                <ProcessingCard task={task} stages={stages} elapsed={elapsed} />
                <LiveActivity task={task} stages={stages} />
              </motion.div>
            )}
          </AnimatePresence>

          {done && task?.result && (
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-xl border border-ai-confident/25 bg-ai-confident/[0.06] px-3.5 py-2.5 text-[13px]">
              <Check className="h-4 w-4 text-ai-confident" />
              <span className="font-medium">{summaryLine(task.result)}</span>
              {task.result.approval_blocked_reason && (
                <p className="basis-full text-xs text-muted-foreground">{plainly(task.result.approval_blocked_reason)}</p>
              )}
            </div>
          )}

          {failed && (
            <div className="rounded-xl border border-ai-blocked/30 bg-ai-blocked/[0.06] px-3.5 py-2.5 text-[13px]">
              <div className="font-medium text-ai-blocked">Couldn't read this template</div>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {task?.error ? `${plainly(task.error)} ` : ""}
                The file itself was uploaded — use “Read again” on its row to try once more.
              </p>
            </div>
          )}

          {error && (
            <div role="alert" className="rounded-xl border border-ai-blocked/30 bg-ai-blocked/[0.06] px-3.5 py-2.5 text-xs text-ai-blocked">
              {error}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="mt-5 flex items-center justify-between gap-3 border-t border-border px-6 py-4">
          <button
            type="button"
            onClick={close}
            disabled={!running}
            title="Close this and keep reading. Progress stays in the corner of the screen."
            className="h-9 rounded-lg border border-border bg-background/40 px-3.5 text-[13px] font-medium text-foreground transition-colors hover:border-border-strong hover:bg-accent disabled:pointer-events-none disabled:opacity-40"
          >
            Run in background
          </button>
          <PrimaryButton
            phase={phase}
            task={task}
            percent={percent}
            canStart={!!file && !!projectId}
            onStart={start}
            onView={viewTemplate}
            onClose={close}
          />
        </div>
      </DialogContent>
    </Dialog>
  );
}

function PrimaryButton({
  phase, task, percent, canStart, onStart, onView, onClose,
}: {
  phase: Phase;
  task: ReadingTask | undefined;
  percent: number;
  canStart: boolean;
  onStart: () => void;
  onView: () => void;
  onClose: () => void;
}) {
  const base =
    "relative inline-flex h-9 min-w-[10.5rem] items-center justify-center gap-2 overflow-hidden rounded-lg px-4 text-[13px] font-semibold text-white bg-gradient-live shadow-[0_8px_24px_-10px_color-mix(in_oklab,var(--ai-live)_70%,transparent)] transition-opacity hover:opacity-95 disabled:cursor-default";
  if (task?.status === "running") {
    return (
      <button type="button" disabled className={cn(base, "opacity-95")} aria-live="polite">
        <Loader2 className="h-4 w-4 animate-spin" />
        Analyzing ({percent}%)…
      </button>
    );
  }
  if (task?.status === "done") {
    return <button type="button" onClick={onView} className={base}>View template</button>;
  }
  if (task?.status === "failed") {
    return <button type="button" onClick={onClose} className={base}>Close</button>;
  }
  return (
    <button type="button" onClick={onStart} disabled={!canStart || phase === "uploading"}
            className={cn(base, "disabled:opacity-45")}>
      {phase === "uploading" && <Loader2 className="h-4 w-4 animate-spin" />}
      {phase === "uploading" ? "Uploading…" : "Upload & read"}
    </button>
  );
}

function StatusPill({ status }: { status: ReadingTask["status"] }) {
  if (status === "running") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-ai-live/35 bg-ai-live/10 px-2 py-0.5 font-mono text-[10px] font-semibold tracking-[0.12em] text-ai-live">
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-ai-live" /> RUNNING
      </span>
    );
  }
  if (status === "done") {
    return (
      <span className="inline-flex items-center rounded-full border border-ai-confident/35 bg-ai-confident/10 px-2 py-0.5 font-mono text-[10px] font-semibold tracking-[0.12em] text-ai-confident">
        COMPLETE
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-full border border-ai-blocked/35 bg-ai-blocked/10 px-2 py-0.5 font-mono text-[10px] font-semibold tracking-[0.12em] text-ai-blocked">
      FAILED
    </span>
  );
}

function ProcessingCard({ task, stages, elapsed }: { task: ReadingTask; stages: ReadingStep[]; elapsed: number | null }) {
  const running = task.status === "running";
  const idx = currentStepIndex({ status: task.status, stages });
  const current = stages[idx];
  const subtitle =
    task.status === "done" ? "Your template is ready to use."
    : task.status === "failed" ? "Reading stopped before it finished."
    : STEP_DESCRIPTION[current?.key ?? "read"] ?? current?.label;

  return (
    <div className={cn(
      "rounded-xl border bg-background/40 p-4 transition-[box-shadow,border-color] duration-300 light:bg-surface",
      running ? "border-ai-live/40 glow-live"
        : task.status === "done" ? "border-ai-confident/30"
        : "border-ai-blocked/30",
    )}>
      <div className="flex items-start gap-3">
        <div className={cn(
          "grid h-9 w-9 shrink-0 place-items-center rounded-lg border",
          running ? "border-ai-live/35 bg-ai-live/10 text-ai-live"
            : task.status === "done" ? "border-ai-confident/35 bg-ai-confident/10 text-ai-confident"
            : "border-ai-blocked/35 bg-ai-blocked/10 text-ai-blocked",
        )}>
          <Zap className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold">Processing your template</span>
            <StatusPill status={task.status} />
          </div>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">{subtitle}</p>
        </div>
        <div className="shrink-0 text-right font-mono leading-tight">
          <div className="text-sm font-semibold tabular-nums">
            {task.status === "done" ? 3 : idx + 1}<span className="text-muted-foreground">/3</span>
          </div>
          <div className="mt-0.5 text-[11px] tabular-nums text-muted-foreground">{formatDuration(elapsed) ?? "—"}</div>
        </div>
      </div>

      <SegmentBar stages={stages} status={task.status} className="mt-3.5" />

      <ul className="mt-3.5 space-y-1">
        {stages.map((s) => <StepRow key={s.key} step={s} />)}
      </ul>
    </div>
  );
}

export function SegmentBar({ stages, status, className, thin }: {
  stages: ReadingStep[];
  status: ReadingTask["status"];
  className?: string;
  thin?: boolean;
}) {
  const steps = stages.length ? stages : DEFAULT_STEPS;
  return (
    <div className={cn("flex gap-1.5", className)} aria-hidden>
      {steps.map((s) => {
        const st = status === "done" ? "done" : s.status;
        return (
          <div
            key={s.key}
            className={cn(
              "flex-1 overflow-hidden rounded-full",
              thin ? "h-1" : "h-1.5",
              st === "done" ? "bg-ai-confident"
                : st === "running" ? "live-stripe"
                : st === "failed" ? "bg-ai-blocked"
                : "bg-muted-foreground/15",
            )}
          />
        );
      })}
    </div>
  );
}

function StepRow({ step }: { step: ReadingStep }) {
  const chip =
    step.status === "done"
      ? { text: [formatDuration(step.elapsed_seconds), "COMPLETE"].filter(Boolean).join(" · "),
          cls: "border-ai-confident/30 bg-ai-confident/10 text-ai-confident" }
      : step.status === "running"
      ? { text: "IN PROGRESS", cls: "border-ai-live/30 bg-ai-live/10 text-ai-live" }
      : step.status === "failed"
      ? { text: "STOPPED", cls: "border-ai-blocked/30 bg-ai-blocked/10 text-ai-blocked" }
      : { text: "QUEUED", cls: "border-transparent text-muted-foreground/70" };
  return (
    <li className="flex items-center gap-2.5 rounded-lg px-1 py-1.5">
      <span className="grid h-5 w-5 shrink-0 place-items-center">
        {step.status === "done" ? (
          <span className="grid h-[18px] w-[18px] place-items-center rounded-full bg-ai-confident/15 text-ai-confident">
            <Check className="h-3 w-3" strokeWidth={3} />
          </span>
        ) : step.status === "running" ? (
          <Loader2 className="h-4 w-4 animate-spin text-ai-live" />
        ) : step.status === "failed" ? (
          <span className="grid h-[18px] w-[18px] place-items-center rounded-full bg-ai-blocked/15 text-ai-blocked">
            <X className="h-3 w-3" strokeWidth={3} />
          </span>
        ) : (
          <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/35" />
        )}
      </span>
      <span className={cn("flex-1 text-[13px]", step.status === "pending" ? "text-muted-foreground" : "text-foreground")}>
        {step.label}
      </span>
      <span className={cn("rounded-md border px-2 py-0.5 font-mono text-[10px] font-medium tracking-wider tabular-nums", chip.cls)}>
        {chip.text}
      </span>
    </li>
  );
}

interface ActivityLine { text: string; state: "past" | "current" | "failed" }

function plural(n: number, one: string, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

/** Only events the server has actually reported, in the order they happen. */
function activityLines(task: ReadingTask, stages: ReadingStep[]): ActivityLine[] {
  const lines: ActivityLine[] = [];
  const size = formatBytes(task.sizeBytes);
  lines.push({ text: size ? `Template uploaded · ${size}` : "Template uploaded", state: "past" });

  const status = (key: string) => (task.status === "done" ? "done" : stages.find((s) => s.key === key)?.status ?? "pending");
  const f = task.facts ?? {};

  lines.push({ text: status("read") === "done" ? "Template read" : "Reading your template…", state: "past" });
  if (f.placeholders_found != null) lines.push({ text: `Found ${plural(f.placeholders_found, "placeholder")}`, state: "past" });
  if (f.instructions_found) lines.push({ text: `Found ${plural(f.instructions_found, "author instruction")}`, state: "past" });
  if (f.word_fields_found) lines.push({ text: `Found ${plural(f.word_fields_found, "Word field")}`, state: "past" });

  if (status("analyse") !== "pending") {
    lines.push({ text: status("analyse") === "done" ? "Structure understood" : "Understanding the structure…", state: "past" });
  }
  if (f.values_found != null) lines.push({ text: `Found ${plural(f.values_found, "value")}`, state: "past" });
  if (f.optional_sections_found != null) {
    lines.push({ text: `Found ${plural(f.optional_sections_found, "optional section")}`, state: "past" });
  }
  if (status("finish") === "running") lines.push({ text: "Finishing up…", state: "past" });

  if (task.status === "done") {
    if (task.result?.warning_count) {
      lines.push({ text: `${plural(task.result.warning_count, "thing")} worth checking`, state: "past" });
    }
    lines.push({ text: "Ready", state: "past" });
  } else if (task.status === "failed") {
    lines.push({ text: "Stopped before it finished", state: "failed" });
  } else {
    // The newest line is the one happening now.
    lines[lines.length - 1] = { ...lines[lines.length - 1], state: "current" };
  }
  return lines;
}

function LiveActivity({ task, stages }: { task: ReadingTask; stages: ReadingStep[] }) {
  const lines = useMemo(() => activityLines(task, stages), [task, stages]);
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "nearest" });
  }, [lines.length]);
  const live = task.status === "running";
  return (
    <div className="rounded-xl border border-border bg-background/70 light:bg-muted/60">
      <div className="flex items-center justify-between border-b border-border/70 px-3.5 py-2">
        <div className="flex items-center gap-2 font-mono text-[10px] font-semibold tracking-[0.16em] text-muted-foreground">
          <span className={cn("h-1.5 w-1.5 rounded-full",
            live ? "animate-pulse bg-ai-confident" : task.status === "failed" ? "bg-ai-blocked" : "bg-ai-confident")} />
          LIVE ACTIVITY
        </div>
        <span className="font-mono text-[10px] tracking-wider text-muted-foreground/60">DocuMind AI</span>
      </div>
      <div className="max-h-60 space-y-1 overflow-y-auto px-3.5 py-2.5 font-mono text-[12px] leading-5">
        {lines.map((l, i) => (
          <div key={`${i}-${l.text}`} className="flex items-start gap-2">
            <span className={cn("w-3 shrink-0 text-center",
              l.state === "current" ? "text-ai-live" : l.state === "failed" ? "text-ai-blocked" : "text-ai-confident")}>
              {l.state === "current" ? "●" : l.state === "failed" ? "✕" : "✓"}
            </span>
            <span className={cn(l.state === "current" ? "text-foreground" : "text-muted-foreground")}>{l.text}</span>
          </div>
        ))}
        <div ref={endRef} />
      </div>
    </div>
  );
}
