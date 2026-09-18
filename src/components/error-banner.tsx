import { motion } from "framer-motion";
import { AlertTriangle, RefreshCcw, X } from "lucide-react";
import { DUR, EASE_OUT, useReducedMotionFlag } from "@/components/motion";
import { cn } from "@/lib/utils";

/**
 * One shape for every failure the user can see.
 *
 * Three fields, and the split between them is the point. `title` is what went
 * wrong in the product's words; `message` is what the person can do about it;
 * `detail` is the server's own text, kept verbatim in monospace rather than
 * paraphrased -- an error code somebody can quote is worth more in a support
 * thread than a friendlier sentence that has lost it.
 *
 * `onRetry` is offered only where retrying is genuinely the fix. A retry button
 * on a validation failure teaches people that the button does nothing.
 */
export function ErrorBanner({
  title,
  message,
  detail,
  onRetry,
  retrying = false,
  onDismiss,
  className,
}: {
  title: string;
  message?: string;
  detail?: string;
  onRetry?: () => void;
  retrying?: boolean;
  onDismiss?: () => void;
  className?: string;
}) {
  // Guarded in code, not only in the stylesheet: the CSS `prefers-reduced-motion`
  // block clamps transitions, not a JavaScript-driven transform -- and this is
  // the element with `role="alert"`, so the one person who asked for no motion
  // would otherwise get an unrequested slide on the thing demanding attention.
  const reduced = useReducedMotionFlag();
  return (
    <motion.div
      role="alert"
      initial={reduced ? { opacity: 0 } : { opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={reduced ? { opacity: 0 } : { opacity: 0, y: -6 }}
      transition={{ duration: reduced ? DUR.micro : DUR.base, ease: EASE_OUT }}
      className={cn(
        "rounded-xl border border-ai-blocked/35 bg-ai-blocked/8 px-4 py-3.5",
        className,
      )}
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-ai-blocked/30 bg-ai-blocked/10 text-ai-blocked">
          <AlertTriangle className="h-4 w-4" />
        </span>

        <div className="min-w-0 flex-1">
          <p className="text-[13.5px] font-semibold leading-snug tracking-tight text-foreground">{title}</p>
          {message && <p className="mt-1 text-[13px] leading-relaxed text-muted-foreground">{message}</p>}
          {detail && (
            <p
              className="mt-1.5 truncate font-mono text-[11px] leading-relaxed text-muted-foreground/80"
              title={detail}
            >
              {detail}
            </p>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          {onRetry && (
            <button
              onClick={onRetry}
              disabled={retrying}
              className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-surface px-3 text-[12.5px] font-medium transition-colors hover:bg-accent disabled:opacity-60"
            >
              <RefreshCcw className={cn("h-3.5 w-3.5", retrying && "animate-spin")} />
              {retrying ? "Retrying…" : "Retry"}
            </button>
          )}
          {onDismiss && (
            <button
              onClick={onDismiss}
              aria-label="Dismiss"
              className="flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>
    </motion.div>
  );
}
