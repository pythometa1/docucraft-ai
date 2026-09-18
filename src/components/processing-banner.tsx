import { motion } from "framer-motion";
import {
  BrainCircuit,
  CheckCircle2,
  Cpu,
  Database,
  FileSearch,
  Sparkles,
  XCircle,
} from "lucide-react";

import { usePageVisible } from "@/components/motion";
import { cn } from "@/lib/utils";
import type { Stage, StageKind } from "@/components/compile-progress";

/**
 * The shared vocabulary for "the engine is working", and the mark that says so.
 *
 * Rendering the run itself belongs to `CompileReveal`; what lives here is the
 * part more than one screen needs to agree on. Two things:
 *
 *  - **What a stage is called, and what sort of work it is.** A template read by
 *    the colour rules costs nothing and finishes instantly; one handed to a model
 *    costs real money and takes minutes. Which of those a run is sitting in
 *    should never be a guess, and the answer must not differ between a dialog
 *    and a table row -- a stage that reads "Reading each part" in one place and
 *    something else in another is two products.
 *  - **Plain language on the way to the screen.** The person watching uploaded a
 *    Word file and is waiting for a Word file; the intermediate structure the
 *    engine builds is our implementation detail, not their vocabulary.
 */

const KIND: Record<StageKind, { icon: typeof Cpu; label: string; hue: string; ring: string }> = {
  deterministic: {
    icon: Cpu,
    label: "On this machine",
    hue: "text-ai-confident",
    ring: "border-ai-confident/45 bg-ai-confident/12",
  },
  retrieval: {
    icon: FileSearch,
    label: "Searching your data",
    hue: "text-purple",
    ring: "border-purple/45 bg-purple/12",
  },
  model: {
    icon: BrainCircuit,
    label: "AI model",
    hue: "text-ai-uncertain",
    ring: "border-ai-uncertain/45 bg-ai-uncertain/12",
  },
  embedding: {
    icon: Database,
    label: "Building the index",
    hue: "text-ai-active",
    ring: "border-ai-active/45 bg-ai-active/12",
  },
};

/**
 * Short verbs for the stages the engine publishes, keyed on the stage id rather
 * than matched against its sentence -- a key is a contract, a label is prose and
 * will be reworded the first time somebody improves it.
 *
 * Anything unrecognised falls through to the label the server sent, run past
 * `plainly()` first so a phrase written for an engineer is not shown to a
 * customer verbatim.
 */
const VERB: Record<string, string> = {
  parse: "Reading the document",
  scan: "Scanning the layout",
  chunk: "Chunking the template",
  write: "Reading each part",
  reconcile: "Reconciling the parts",
  review: "Reviewing what it found",
  retrieval: "Matching your column names",
  agentic: "Processing the template",
  embedding: "Indexing for next time",
};

/** Strips internal vocabulary out of any text on its way to the screen.
 *
 *  This runs on the client on purpose. The alternative was rewording the
 *  server's stage labels, and those same strings are read by the compile
 *  transcript, the audit row and the tests -- so renaming them for the benefit
 *  of a banner would have changed a record somebody may have to defend later.
 *  Presentation is the right place for a presentation problem. */
export function plainly(text: string): string {
  return text
    .replace(/compil(e|ing|ed|ation)\s+the\s+manifest/gi, "reading the template")
    .replace(/manifest\s+generation/gi, "processing")
    .replace(/generat(e|ing)\s+(the\s+)?manifest/gi, "processing the template")
    .replace(/\bmanifests?\b/gi, "reading")
    .replace(/\bcompil(e|ing)\b/gi, "processing")
    .replace(/^(.)/, (m) => m.toUpperCase());
}

function label(stage: Stage): string {
  return VERB[stage.key] ?? plainly(stage.label ?? "Processing");
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

/* The stage vocabulary, shared.
 *
 * `CompileReveal` renders the stages and imports the naming from here, alongside
 * `plainly`, which five other screens import as well. Exported from here rather
 * than moved: the dependency runs one way, reveal -> vocabulary, and there is no
 * cycle to reason about. */
export { KIND as STAGE_KIND, label as stageLabel };
