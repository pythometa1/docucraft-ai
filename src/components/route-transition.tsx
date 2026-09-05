import { motion } from "framer-motion";
import type { ReactNode } from "react";
import { useRouterState } from "@tanstack/react-router";
import { DUR, EASE_OUT, useReducedMotionFlag } from "@/components/motion";

/**
 * A short fade and eight-pixel rise on the page body, keyed on the path.
 *
 * Content only. The sidebar, the header and the atmosphere sit outside it and
 * never re-animate -- a shell that fades on every navigation reads as a full
 * page load, which is the one impression a single-page app exists to avoid.
 */
export function RouteTransition({ children }: { children: ReactNode }) {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const reduced = useReducedMotionFlag();

  return (
    <motion.div
      key={pathname}
      className="min-w-0"
      initial={reduced ? { opacity: 0 } : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: reduced ? DUR.micro : DUR.route, ease: EASE_OUT }}
    >
      {children}
    </motion.div>
  );
}
