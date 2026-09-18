import { motion } from "framer-motion";
import type { ReactNode } from "react";
import { DUR, EASE_OUT, useReducedMotionFlag } from "@/components/motion";
import { cn } from "@/lib/utils";

/**
 * Loading placeholders that keep the layout still.
 *
 * A skeleton is worth having only when it occupies the space the real content
 * will, so nothing jumps when the request lands. A spinner in the middle of an
 * empty panel guarantees the opposite.
 */

/** One shimmering bar. */
export function SkeletonBar({ className }: { className?: string }) {
  return <div className={cn("ai-skeleton h-3.5 w-full", className)} />;
}

/** Placeholder rows for a table body, staggered so the block settles rather
 *  than appearing all at once. */
export function TableSkeleton({ rows = 6, cols = 6 }: { rows?: number; cols?: number }) {
  const widths = ["w-40", "w-24", "w-28", "w-24", "w-20", "w-16", "w-24", "w-20"];
  return (
    <tbody>
      {Array.from({ length: rows }).map((_, r) => (
        <tr key={r} className="border-t border-border/70">
          {Array.from({ length: cols }).map((_, c) => (
            <td key={c} className="px-4 py-4">
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ duration: DUR.base, ease: EASE_OUT, delay: Math.min(r, 12) * 0.04 }}
              >
                <SkeletonBar className={widths[c] ?? "w-24"} />
              </motion.div>
            </td>
          ))}
        </tr>
      ))}
    </tbody>
  );
}

/** Placeholder for a card grid — templates, projects. */
export function CardGridSkeleton({ count = 6 }: { count?: number }) {
  const reduced = useReducedMotionFlag();
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {Array.from({ length: count }).map((_, i) => (
        <motion.div
          key={i}
          initial={reduced ? { opacity: 0 } : { opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{
            duration: reduced ? DUR.micro : DUR.reveal,
            ease: EASE_OUT,
            delay: Math.min(i, 12) * 0.04,
          }}
          className="surface-raised rounded-xl p-5"
        >
          <div className="flex items-center gap-3">
            <SkeletonBar className="h-10 w-10 rounded-xl" />
            <div className="flex-1 space-y-2">
              <SkeletonBar className="h-3 w-28" />
              <SkeletonBar className="h-2.5 w-20" />
            </div>
          </div>
          <div className="mt-5 space-y-2.5">
            <SkeletonBar className="h-2.5 w-full" />
            <SkeletonBar className="h-2.5 w-[82%]" />
          </div>
        </motion.div>
      ))}
    </div>
  );
}

/** Placeholder for a stage panel body. */
export function StageSkeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <SkeletonBar className="h-10 w-10 rounded-xl" />
        <div className="flex-1 space-y-2">
          <SkeletonBar className="h-3 w-40" />
          <SkeletonBar className="h-3 w-64" />
        </div>
      </div>
      <div className="space-y-2.5">
        {Array.from({ length: lines }).map((_, i) => (
          <SkeletonBar key={i} className={cn("h-12 rounded-xl", i % 2 ? "w-full" : "w-[92%]")} />
        ))}
      </div>
    </div>
  );
}

/** A centred empty state with a soft halo behind the icon and room for one
 *  action. Used where there is genuinely nothing yet, never where a request
 *  failed -- that is `ErrorBanner`'s job, and conflating the two tells somebody
 *  their data is missing when the server merely timed out. */
export function PolishedEmpty({
  icon,
  title,
  subtitle,
  action,
  className,
}: {
  icon: ReactNode;
  title: string;
  subtitle: string;
  action?: ReactNode;
  className?: string;
}) {
  const reduced = useReducedMotionFlag();
  return (
    <motion.div
      initial={reduced ? { opacity: 0 } : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: reduced ? DUR.micro : DUR.reveal, ease: EASE_OUT }}
      className={cn(
        "flex flex-col items-center justify-center rounded-2xl border border-dashed border-border/80 bg-surface-elevated/30 px-6 py-14 text-center",
        className,
      )}
    >
      <div className="relative mb-5 flex h-14 w-14 items-center justify-center rounded-2xl border border-border/70 bg-surface text-brand">
        <div className="absolute inset-0 rounded-2xl bg-brand/10 blur-md" />
        <div className="relative">{icon}</div>
      </div>
      <h3 className="text-base font-semibold tracking-tight">{title}</h3>
      <p className="mt-1.5 max-w-sm text-sm leading-relaxed text-muted-foreground">{subtitle}</p>
      {action && <div className="mt-6">{action}</div>}
    </motion.div>
  );
}
