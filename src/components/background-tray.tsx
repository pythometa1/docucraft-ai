import { useEffect, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { AnimatePresence, motion } from "framer-motion";
import { Check, Loader2, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { EASE_OUT, useReducedMotionFlag } from "@/components/motion";
import {
  currentStepIndex,
  registerNavigator,
  summaryLine,
  useBackgroundTasks,
  type ReadingTask,
} from "@/lib/background-tasks";
import { SegmentBar, TemplateUploadDialog } from "@/components/template-upload-dialog";

/**
 * Readings sent to the background, in the corner of every screen.
 *
 * Mounted once in the app shell so it survives route changes. Clicking a card
 * re-opens the upload dialog attached to that reading; the tray is absent when
 * nothing is running.
 */
export function BackgroundTray() {
  const navigate = useNavigate();
  const reduced = useReducedMotionFlag();
  const tasks = useBackgroundTasks((s) => s.tasks);
  const watching = useBackgroundTasks((s) => s.watching);
  const [openToken, setOpenToken] = useState<string | null>(null);

  // The store's toasts navigate through the router rather than reloading.
  useEffect(() => {
    registerNavigator((projectId) => void navigate({ to: "/projects/$id", params: { id: projectId } }));
    return () => registerNavigator(null);
  }, [navigate]);

  // A task the dialog is showing is not repeated in the corner.
  const visible = Object.values(tasks)
    .filter((t) => t.token !== watching)
    .sort((a, b) => a.startedAt - b.startedAt);

  return (
    <>
      <div className="pointer-events-none fixed bottom-4 right-4 z-40 flex w-[19rem] flex-col gap-2">
        <AnimatePresence initial={false}>
          {visible.map((t) => (
            <motion.div
              key={t.token}
              layout={!reduced}
              initial={reduced ? false : { opacity: 0, y: 12, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={reduced ? { opacity: 0 } : { opacity: 0, y: 8, scale: 0.98 }}
              transition={{ duration: 0.22, ease: EASE_OUT }}
              className="pointer-events-auto"
            >
              <TrayCard task={t} onOpen={() => setOpenToken(t.token)} />
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
      <TemplateUploadDialog
        open={openToken != null}
        onOpenChange={(v) => { if (!v) setOpenToken(null); }}
        attachToken={openToken}
      />
    </>
  );
}

function TrayCard({ task, onOpen }: { task: ReadingTask; onOpen: () => void }) {
  const dismiss = useBackgroundTasks((s) => s.dismiss);
  const running = task.status === "running";
  const step = currentStepIndex(task) + 1;
  const line =
    running ? `Reading · step ${step} of 3`
    : task.status === "done" ? summaryLine(task.result) || "Ready"
    : "Couldn't be read";

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); } }}
      className={cn(
        "group cursor-pointer rounded-xl border bg-surface/95 p-3 backdrop-blur transition-colors",
        "shadow-[0_12px_32px_-16px_oklch(0_0_0/0.55)]",
        running ? "border-ai-live/35 hover:border-ai-live/60"
          : task.status === "done" ? "border-ai-confident/35" : "border-ai-blocked/35",
      )}
      title="Show progress"
    >
      <div className="flex items-center gap-2.5">
        <span className={cn(
          "grid h-7 w-7 shrink-0 place-items-center rounded-lg border",
          running ? "border-ai-live/30 bg-ai-live/10 text-ai-live"
            : task.status === "done" ? "border-ai-confident/30 bg-ai-confident/10 text-ai-confident"
            : "border-ai-blocked/30 bg-ai-blocked/10 text-ai-blocked",
        )}>
          {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
            : task.status === "done" ? <Check className="h-3.5 w-3.5" strokeWidth={3} />
            : <X className="h-3.5 w-3.5" strokeWidth={3} />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate font-mono text-[12px] font-medium" title={task.templateName}>{task.templateName}</div>
          <div className="truncate text-[11px] text-muted-foreground">{line}</div>
        </div>
        {running ? (
          <span className="shrink-0 font-mono text-[11px] tabular-nums text-muted-foreground">{step}/3</span>
        ) : (
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); dismiss(task.token); }}
            className="shrink-0 rounded p-1 text-muted-foreground opacity-60 hover:bg-accent hover:opacity-100"
            aria-label="Dismiss"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      <SegmentBar stages={task.stages} status={task.status} thin className="mt-2.5" />
    </div>
  );
}
