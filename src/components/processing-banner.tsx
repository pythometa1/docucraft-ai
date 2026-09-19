import { motion } from "framer-motion";
import { CheckCircle2, Sparkles, XCircle } from "lucide-react";

import { usePageVisible } from "@/components/motion";
import { cn } from "@/lib/utils";
import type { Stage } from "@/components/compile-progress";

/**
 * The shared vocabulary for "the engine is working", and the mark that says so.
 *
 * Rendering the run itself belongs to `CompileReveal`; what lives here is the
 * part more than one screen needs to agree on: what a step is called, and plain
 * language on the way to the screen. The person watching uploaded a Word file
 * and is waiting for a Word file; how the engine reads it is our business, so
 * no step says what kind of work it is.
 */

/** Strips internal vocabulary out of any text on its way to the screen.
 *
 *  This runs on the client on purpose. The alternative was rewording the
 *  server's stage labels, and those same strings are read by the compile
 *  transcript, the audit row and the tests -- so renaming them for the benefit
 *  of a banner would have changed a record somebody may have to defend later.
 *  Presentation is the right place for a presentation problem. */
export function plainly(text: string): string {
  return String(text ?? "")
    .replace(/compil(e|ing|ed|ation)\s+the\s+manifest/gi, "reading the template")
    .replace(/manifest\s+generation/gi, "processing")
    .replace(/generat(e|ing)\s+(the\s+)?manifest/gi, "processing the template")
    .replace(/\bmanifests?\b/gi, "template")
    .replace(/\b(re-?)?compil(e|ing|ed|er|ation)s?\b/gi, "processing")
    // Section references point at an internal spec nobody outside can read.
    .replace(/\s*\(?§\s*\d+(\.\d+)*\)?/g, "")
    .replace(/\b(in |after |on )?(round|pass|attempt) \d+( of \d+)?\b/gi, "")
    .replace(/\b(the )?(language )?model'?s?\b|\bLLMs?\b|\bAI model\b/gi, "the service")
    .replace(/\bcosine( similarity)?\b|\bsimilarity score\b/gi, "match")
    .replace(/\bembeddings?\b|\bembedded\b|\bvectors?\b/gi, "search index")
    .replace(/\bchunks?\b|\bchunked\b|\bchunking\b/gi, "passages")
    .replace(/\bfingerprints?\b/gi, "check")
    .replace(/\b(the )?engine's\b/gi, "our")
    .replace(/\b(the )?engine\b/gi, "we")
    .replace(/\s{2,}/g, " ")
    .replace(/\s+([.,;:])/g, "$1")
    .trim()
    .replace(/^(.)/, (m) => m.toUpperCase());
}

/** The server already names each step for a reader; `plainly()` is a guard. */
function label(stage: Stage): string {
  return plainly(stage.label ?? "Processing");
}

/** The mark at the top-left of a run: a rounded tile with a soft halo, three
 *  orbiting dots while work is running, and a settled icon once it is not.
 *
 *  Both loops are infinite, so both are held behind two switches: the caller's
 *  `reduced` flag, and whether the tab is actually in front. A compositor
 *  animation nobody can see is battery spend with no upside, and this mark is by
 *  definition on screen during the long jobs -- which is exactly when somebody
 *  switches away to do something else. */
export function ProcessingMark({ state, reduced }: { state: string; reduced: boolean }) {
  const visible = usePageVisible();
  const Icon = state === "failed" ? XCircle : state === "done" ? CheckCircle2 : Sparkles;

  return (
    <div className="relative flex h-10 w-10 shrink-0 items-center justify-center">
      {state === "running" && !reduced && visible && (
        <>
          <motion.span
            aria-hidden
            className="absolute inset-0 rounded-xl bg-ai-active/20 blur-md"
            animate={{ opacity: [0.35, 0.75, 0.35] }}
            transition={{ duration: 2.2, repeat: Infinity, ease: "easeInOut" }}
          />
          <motion.span
            aria-hidden
            className="absolute inset-[-5px]"
            animate={{ rotate: 360 }}
            transition={{ duration: 6, repeat: Infinity, ease: "linear" }}
          >
            {[0, 120, 240].map((deg) => (
              <span
                key={deg}
                className="absolute left-1/2 top-1/2 h-1 w-1 rounded-full bg-ai-active/80"
                style={{ transform: `rotate(${deg}deg) translateY(-1.55rem)` }}
              />
            ))}
          </motion.span>
        </>
      )}

      <div
        className={cn(
          "relative flex h-10 w-10 items-center justify-center rounded-xl border",
          state === "failed"
            ? "border-ai-blocked/40 bg-ai-blocked/12 text-ai-blocked"
            : state === "done"
              ? "border-ai-confident/40 bg-ai-confident/12 text-ai-confident"
              : "border-ai-active/40 bg-ai-active/12 text-ai-active",
        )}
      >
        <Icon className="h-[18px] w-[18px]" />
      </div>
    </div>
  );
}

/* The step naming, shared with `CompileReveal`. */
export { label as stageLabel };
