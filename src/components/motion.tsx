/**
 * The app's motion vocabulary, in one place.
 *
 * `framer-motion` has been a dependency since the project was set up and had
 * exactly zero imports, so every screen was static. These are the three
 * movements worth having and nothing else -- a panel arriving, a list settling,
 * and a number changing -- because motion that is invented per screen is how an
 * interface ends up with six different ideas about how fast things move.
 *
 * Every one of them is disabled under `prefers-reduced-motion`. That is enforced
 * here in code as well as in the stylesheet, because a transform animation
 * driven by JavaScript does not go away when CSS transitions are turned off --
 * and vestibular triggers are the reason the setting exists, not a preference
 * about polish.
 */

import { type ReactNode } from "react";
import { motion, useReducedMotion, type Transition } from "framer-motion";

import { staggerDelay } from "@/lib/motion";

/** Slightly overdamped, and short. Anything springier reads as a toy in a
 *  product about regulated documents. */
const EASE: Transition = { duration: 0.28, ease: [0.22, 0.61, 0.36, 1] };

/** A panel arriving: up a few pixels, in from nothing. */
export function FadeIn({ children, className, delay = 0 }: {
  children: ReactNode;
  className?: string;
  delay?: number;
}) {
  const still = useReducedMotion();
  if (still) return <div className={className}>{children}</div>;
  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ ...EASE, delay }}
    >
      {children}
    </motion.div>
  );
}

/** A list settling in, each row a beat behind the one above.
 *
 *  The step this container hands out is `staggerChildren`, which is strictly
 *  proportional: row N starts N steps late, with no ceiling. That is fine for a
 *  fixed handful of tiles and wrong for a list whose length comes off the
 *  server, so the ceiling lives on the item instead -- a `StaggerItem` given an
 *  `index` declares its own capped delay, and a delay declared on the child wins
 *  over the offset declared here. See `StaggerItem`. */
export function Stagger({ children, className }: { children: ReactNode; className?: string }) {
  const still = useReducedMotion();
  if (still) return <div className={className}>{children}</div>;
  return (
    <motion.div
      className={className}
      initial="hidden"
      animate="shown"
      variants={{
        hidden: {},
        shown: { transition: { staggerChildren: 0.035, delayChildren: 0.02 } },
      }}
    >
      {children}
    </motion.div>
  );
}

/** One row of a `Stagger`. Exactly two behaviours, and which one you get depends
 *  on whether you pass `index`:
 *
 *  - **With `index`:** the row's delay is `staggerDelay(index)` -- `CAP.stepMs`
 *    per row up to `CAP.staggerItems`, and zero for every row past it, so a
 *    thousand-row list finishes arriving in under half a second instead of
 *    taking half a minute. The delay is carried on the variant's own transition,
 *    which overrides the container's proportional offset rather than adding to
 *    it, so the cap is real and not just a comment.
 *  - **Without `index`:** the row inherits the container's proportional
 *    `staggerChildren` step, uncapped. Kept as the default because most call
 *    sites render a short fixed list and pass no index. Pass one for any list
 *    whose length the server decides. */
export function StaggerItem({
  children,
  className,
  index,
}: {
  children: ReactNode;
  className?: string;
  index?: number;
}) {
  const still = useReducedMotion();
  if (still) return <div className={className}>{children}</div>;
  return (
    <motion.div
      className={className}
      custom={index}
      variants={{
        hidden: { opacity: 0, y: 6 },
        shown: (i?: number) => ({
          opacity: 1,
          y: 0,
          transition: i == null ? EASE : { ...EASE, delay: staggerDelay(i) },
        }),
      }}
    >
      {children}
    </motion.div>
  );
}

/** Re-runs its entrance whenever `k` changes. For a panel that swaps content in
 *  place -- the project pipeline's stages -- where there is no mount to hook. */
export function SwapIn({ k, children, className }: {
  k: string | number;
  children: ReactNode;
  className?: string;
}) {
  const still = useReducedMotion();
  if (still) return <div className={className}>{children}</div>;
  return (
    <motion.div
      key={k}
      className={className}
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={EASE}
    >
      {children}
    </motion.div>
  );
}

/* ---------------------------------------------------------------------------
   The rest of the vocabulary lives in `@/lib/motion`.

   Durations, springs, staggers and the hooks are plain TypeScript and belong in
   lib; only the four JSX primitives above need to be a .tsx. They are re-exported
   here so `@/components/motion` remains a complete import — fourteen files were
   already importing constants and hooks from this path, and a move that breaks
   fourteen imports to gain a directory is not a refactor, it is a chore.
   --------------------------------------------------------------------------- */

export {
  DUR,
  CAP,
  EASE_OUT,
  EASE_IN_OUT,
  SPRING_UI,
  SPRING_PANEL,
  SPRING_PROGRESS,
  SPRING_POP,
  staggerDelay,
  useReducedMotionFlag,
  usePageVisible,
  useCountUp,
  useStagedReveal,
  useElapsed,
} from "@/lib/motion";
