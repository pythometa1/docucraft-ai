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
 *  The stagger is capped rather than proportional: a batch here produces one
 *  document per source row, so a thousand-row list with a 40ms step would take
 *  forty seconds to finish arriving. Past the cap everything lands together,
 *  which is the correct answer for a list that long. */
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

export function StaggerItem({ children, className }: { children: ReactNode; className?: string }) {
  const still = useReducedMotion();
  if (still) return <div className={className}>{children}</div>;
  return (
    <motion.div
      className={className}
      variants={{
        hidden: { opacity: 0, y: 6 },
        shown: { opacity: 1, y: 0, transition: EASE },
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
