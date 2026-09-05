/**
 * Document Mapping — pointing a template's fields at a spreadsheet's columns.
 *
 * Two steps, where there were four:
 *
 *   1  Map       bind each field to a column of the spreadsheet
 *   2  Generate  produce one document per row
 *
 * The other two are gone rather than moved. **Compile** was a button offering to
 * pay for a model call to re-learn what was already known: a template is read
 * when it is uploaded, and reading it again belongs on the template's own row
 * where the consequence is visible. **Review and approve** was a gate that could
 * not be passed without going through a review nobody had asked for --
 * `validate_manifest` turns every unacknowledged compiler warning into a
 * failure, and a freshly read template has warnings and no acknowledgements by
 * construction, so the real sequence was "acknowledge each warning in writing,
 * sign, then generate" for every template. Generating no longer asks whether
 * somebody signed the reading; it asks whether the reading is usable and
 * current.
 *
 * What survives from that step is the part worth keeping: the placeholders the
 * compiler could not claim are shown beside the Generate button, because each
 * one is a document that will come back blocked with "Leftover placeholder
 * brackets". They are advice, not a gate — the person about to press Generate is
 * the one who can judge them.
 *
 * The bands come from §13, and a mapping in REVIEW is not an error -- it is the
 * system saying a person should look before it writes somebody's salary into a
 * letter.
 *
 * Nothing on this screen names the manifest. The identifiers below still do,
 * because that is what the endpoints are called and renaming them here would
 * only hide which request a line makes; what the customer reads is the reading
 * of their template, which is the only part of it they ever asked for.
 *
 * ---
 *
 * **Why the mapping is drawn as two columns and a set of curves.** The old row
 * was `field  ->  [select]  BAND`, which reads as a form and hides the one thing
 * the reader is actually checking: whether the *shape* of the mapping is right.
 * A field pointing at the wrong column is invisible in a list of dropdowns and
 * obvious the moment the lines cross. So the template's fields are one column,
 * the spreadsheet's real headers are the other, and a curve runs between them
 * whose weight and dash carry the band the scorer actually returned. The select
 * is still there and still the control -- the drawing is a second reading of the
 * same state, not a replacement for it.
 *
 * **What is deliberately not drawn.** `binding-suggestions` is one synchronous
 * request with no progress feed, so there is no pipeline to narrate and none is
 * invented: while it is in flight the board shows a skeleton in the geometry the
 * real rows arrive in, and the story of how the number was reached is told by
 * `confidence_policy`, which is a fact the server sends rather than a sequence we
 * mimed. Nothing here reports a stage, a count or a signal the response did not
 * carry: a field whose column was chosen by hand has no measured score, and it
 * says so instead of showing a zero.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { AnimatePresence, motion } from "framer-motion";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  Download,
  FileText,
  Loader2,
  RefreshCw,
  Table2,
  Wand2,
} from "lucide-react";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { type BatchWatch } from "@/components/batch-progress";
import { ErrorBanner } from "@/components/error-banner";
import {
  DUR,
  EASE_IN_OUT,
  EASE_OUT,
  FadeIn,
  staggerDelay,
  useCountUp,
  usePageVisible,
  useReducedMotionFlag,
} from "@/components/motion";
import { plainly } from "@/components/processing-banner";
import { PolishedEmpty, SkeletonBar } from "@/components/skeletons";
import { cn } from "@/lib/utils";

type Step = 1 | 2;

/** A source value that selects none of the branches the template offers. */
type UnmatchedValue = {
  field_id: string;
  column: string | null;
  observed_value: string;
  offered: string[];
};

type ManifestWarning = {
  code?: string;
  message?: string;
  detail?: string;
  evidence?: string;
  paragraph_index?: number;
};

/** One scored (field, column) pairing, exactly as `binding-suggestions` sends
 *  it: the leader carries these on the suggestion itself, every other candidate
 *  carries them in `alternatives`. Both are the same four facts, so the screen
 *  reads them through one shape and never has to care which list a number came
 *  from. `vetoes` are bare codes here because that is how alternatives serialise;
 *  the leader's `{code, explanation}` objects are flattened into the same form
 *  and their explanations kept in a lexicon. */
type Candidate = {
  source_ref: string;
  score: number;
  band: string;
  evidence: string[];
  vetoes: string[];
};

type Veto = { code: string; explanation?: string };

type Suggestion = {
  field_id: string;
  column: string | null;
  confidence: number;
  score?: number;
  band?: string;
  vetoes?: Veto[];
  evidence?: string[];
  alternatives?: Candidate[];
  method?: string;
  rationale?: string;
  origin?: string;
  type?: string;
  declared_type?: string | null;
  observed_type?: string | null;
  sample_value?: string | null;
};

/** What the response says about how its own numbers were produced. Sent on
 *  every `binding-suggestions` reply; the shared API client types the response
 *  down to the keys the previous screen used, so it is widened where it is read
 *  rather than by editing a client four other screens share. */
type ConfidencePolicy = {
  weights_calibrated?: boolean;
  signals_computed?: Record<string, string>;
  signals_not_computed?: Record<string, string>;
  max_attainable_score?: number;
  auto_accept_reachable?: boolean;
  bands?: { auto_accept?: number; confirm?: number; review?: number };
};

/** The bands are painted from the app's intelligence ramp rather than from a
 *  palette of their own, so "sure", "unsure" and "stopped" look the same here as
 *  they do on the processing banner and on a document's status chip. A reader
 *  learns the colour once.
 *
 *  CONFIRM is the working colour rather than a second green: the engine has an
 *  answer and is waiting on a person, which is a different thing from having
 *  applied one. */
const BAND_STYLE: Record<string, string> = {
  AUTO_ACCEPT: "border-ai-confident/30 bg-ai-confident/10 text-ai-confident",
  CONFIRM: "border-ai-active/30 bg-ai-active/10 text-ai-active",
  REVIEW: "border-ai-uncertain/30 bg-ai-uncertain/10 text-ai-uncertain",
  BLOCK: "border-ai-blocked/30 bg-ai-blocked/10 text-ai-blocked",
};

const BAND_MEANING: Record<string, string> = {
  AUTO_ACCEPT: "Strong evidence. Applied automatically, still reversible.",
  CONFIRM: "Good evidence. Pre-selected — one click to accept.",
  REVIEW: "Worth a look before this writes into a letter.",
  BLOCK: "No usable evidence. Pick a column by hand.",
};

const BAND_ORDER = ["AUTO_ACCEPT", "CONFIRM", "REVIEW", "BLOCK"] as const;

/** Not a band. A column somebody chose by hand that was never one of this
 *  field's candidates has no score, no band and no evidence -- and the honest
 *  drawing of "nothing was measured" is the idle colour, not a fifth verdict. */
const UNSCORED = "UNSCORED";

/** How a connector is drawn, per band. The line is the band: weight, dash and
 *  colour together, so the shape of the mapping can be read without reading a
 *  single pill. Colours come from the intelligence ramp as CSS variables because
 *  an SVG stroke cannot take a Tailwind text colour. */
const BAND_LINE: Record<string, {
  colour: string; width: number; opacity: number; dashed: boolean; pulse: boolean;
}> = {
  AUTO_ACCEPT: { colour: "var(--ai-confident)", width: 2.1, opacity: 1, dashed: false, pulse: false },
  CONFIRM: { colour: "var(--ai-active)", width: 1.6, opacity: 0.78, dashed: false, pulse: false },
  REVIEW: { colour: "var(--ai-uncertain)", width: 1.4, opacity: 0.7, dashed: true, pulse: true },
  BLOCK: { colour: "var(--ai-blocked)", width: 2.6, opacity: 0.9, dashed: false, pulse: false },
  [UNSCORED]: { colour: "var(--ai-idle)", width: 1.1, opacity: 0.5, dashed: false, pulse: false },
};

/** The dashed band's pulse, precomputed per band. A keyframe array built during
 *  render is a new target every render, so pointing at another field would
 *  restart every pulse on the board -- with its delay reapplied, which reads as
 *  the lines stuttering. One array per band, made once. */
const PULSE_KEYFRAMES: Record<string, number[]> = Object.fromEntries(
  Object.entries(BAND_LINE).map(([band, line]) => [
    band,
    [line.opacity * 0.5, line.opacity, line.opacity * 0.5],
  ]),
);

/** Short names for §13's signals. The server sends the identifier and a
 *  paragraph of prose; the identifier is an engineer's word and the paragraph is
 *  too long for a chip, so the chip gets one of these and the paragraph goes in
 *  the tooltip and the policy panel, unaltered except for the rewrites below. */
const SIGNAL_LABEL: Record<string, string> = {
  exact_name_match: "Name match",
  semantic_similarity: "Name meaning",
  historical_approvals: "Precedent here",
  type_compatibility: "Type check",
  sentence_context_match: "Sentence around the placeholder",
  family_inheritance: "Similar templates",
  business_rule_consistency: "Fits the rest of the template",
};

/** The order the four live signals are shown in: strongest kind of evidence
 *  first, so a row scanned left to right degrades the same way every time. */
const COMPUTED_SIGNALS = [
  "exact_name_match",
  "semantic_similarity",
  "historical_approvals",
  "type_compatibility",
] as const;

/** `plainly()` handles the compiler's vocabulary. The confidence policy carries
 *  a second one: it is written for whoever maintains the scorer, so it cites
 *  module paths, record sections and the signal identifiers by name. Those are
 *  swapped for what they hold and nothing else is touched — the whole point of
 *  the panel is that the provenance of the number is stated on the record, and
 *  paraphrasing the provenance would defeat it. */
const INTERNAL_NAMES: [RegExp, string][] = [
  [/app\.retrieval\.mapping_memory/g, "this organisation's own approval history"],
  [/app\.retrieval\.vector/g, "the text-similarity index"],
  [/§7's expression layer/g, "the rule layer"],
  [/§\d+/g, "the scoring rule set"],
  [/\bmanifest_bindings\b/g, "the saved column mappings"],
  [/\bhistorical_approvals\b/g, "past approvals"],
];

function readable(text: string): string {
  let out = text;
  for (const [pattern, replacement] of INTERNAL_NAMES) out = out.replace(pattern, replacement);
  return plainly(out);
}

function Pill({ band }: { band?: string }) {
  if (!band) return null;
  return (
    <span
      title={BAND_MEANING[band] ?? band}
      className={cn("shrink-0 rounded-md border px-1.5 py-0.5 text-[10px] font-medium", BAND_STYLE[band] ?? "")}
    >
      {band.replace("_", " ")}
    </span>
  );
}

function StepHeader({ step, active, done, title, hint }: {
  step: Step; active: boolean; done: boolean; title: string; hint: string;
}) {
  return (
    <div className="flex items-center gap-3">
      <div className={cn(
        "flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold",
        done
          ? "bg-ai-confident text-background"
          : active ? "bg-gradient-brand text-white" : "bg-muted text-muted-foreground",
      )}>
        {done ? <CheckCircle2 className="h-4 w-4" /> : step}
      </div>
      <div>
        <div className="text-sm font-medium">{title}</div>
        <div className="text-xs text-muted-foreground">{hint}</div>
      </div>
    </div>
  );
}

/** What the engine took from the template, in one line.
 *
 *  A component of its own for two reasons: `useCountUp` is a hook and this line
 *  renders behind a conditional, and the meter and the figure are driven by the
 *  same number, so the bar fills as the percentage climbs instead of snapping to
 *  a width the text has not reached yet. */
function ReadingSummary({ fields, conditions, confidence, readBy }: {
  fields: number;
  conditions: number;
  /** Null where the reading recorded no figure. Not zero -- see below. */
  confidence: number | null;
  readBy?: string;
}) {
  const measured = confidence != null;
  const fieldCount = Math.round(useCountUp(fields));
  const conditionCount = Math.round(useCountUp(conditions));
  // `enabled` is the whole point of the guard: a reading with no recorded
  // confidence used to arrive here as `?? 0`, and a meter filling to nothing
  // beside "0% confident" says the engine measured this template and had no
  // faith in it. What actually happened is that nobody wrote a number down, so
  // the line says that instead.
  const percent = Math.round(useCountUp(Math.round((confidence ?? 0) * 100), 700, measured));

  return (
    <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-xs text-muted-foreground">
      <span>
        <span className="font-medium tabular-nums text-foreground">{fieldCount}</span> fields
      </span>
      <span aria-hidden className="text-border-strong">·</span>
      <span>
        <span className="font-medium tabular-nums text-foreground">{conditionCount}</span> conditions
      </span>
      {readBy && (
        <>
          <span aria-hidden className="text-border-strong">·</span>
          <span>read by <span className="font-mono text-foreground">{readBy}</span></span>
        </>
      )}
      <span aria-hidden className="text-border-strong">·</span>
      {measured ? (
        <span className="inline-flex items-center gap-1.5">
          {/* scaleX rather than width: a bar that is laid out again on every frame
              is the one animation on this screen that can drop frames, and the
              transform is composited. */}
          <span aria-hidden className="h-1 w-14 overflow-hidden rounded-full bg-border">
            <span
              className="block h-full origin-left rounded-full bg-ai-confident"
              style={{ transform: `scaleX(${percent / 100})` }}
            />
          </span>
          <span className="tabular-nums">{percent}% confident</span>
        </span>
      ) : (
        <span title="Nothing in this reading recorded one, which is not the same as a low one.">
          no confidence figure recorded
        </span>
      )}
    </div>
  );
}

/** Dispositions are keyed by warning code, so one judgement answers every
 *  occurrence of that code. Grouping here says so on screen instead of offering
 *  five buttons that all do the same thing. */
function groupWarnings(warnings: ManifestWarning[]) {
  const groups = new Map<string, { code: string; message: string; occurrences: ManifestWarning[] }>();
  for (const w of warnings) {
    const code = w?.code ?? "UNKNOWN";
    const group = groups.get(code) ?? { code, message: "", occurrences: [] };
    if (!group.message && w?.message) group.message = w.message;
    group.occurrences.push(w);
    groups.set(code, group);
  }
  return [...groups.values()];
}

/* -------------------------------------------------------------------------- */
/* Reading the response                                                       */
/* -------------------------------------------------------------------------- */

/** The scored candidate for a (field, column) pairing, or null when the server
 *  never scored that pairing.
 *
 *  Null is the important return. A suggestion with no column carries
 *  `confidence: 0.0`, and that zero is "there was no column", not "we measured
 *  this pairing and it came to nothing" -- showing it as a score would be the
 *  screen inventing a measurement. Likewise a column somebody picked by hand
 *  that was never proposed has no entry anywhere in the response, so there is
 *  nothing to show and the row says as much. */
function measuredFor(s: Suggestion, column: string | undefined): Candidate | null {
  if (!column) return null;
  if (s.column && column === s.column) {
    return {
      source_ref: column,
      score: s.score ?? s.confidence,
      band: s.band ?? "",
      evidence: s.evidence ?? [],
      vetoes: (s.vetoes ?? []).map((v) => v.code),
    };
  }
  return (s.alternatives ?? []).find((a) => a.source_ref === column) ?? null;
}

/** `name` -> the detail the server attached, or null when the signal fired at
 *  full strength and carried no detail. Undefined means the signal is not in
 *  this candidate's evidence at all, which is a different statement again. */
function evidenceDetail(evidence: string[], name: string): string | null | undefined {
  for (const entry of evidence) {
    if (entry === name) return null;
    if (entry.startsWith(`${name}:`)) return entry.slice(name.length + 1);
  }
  return undefined;
}

type SignalState = "fired" | "measured" | "none" | "absent";

type SignalChip = { name: string; state: SignalState; text: string; title: string };

/** One chip per live signal, from the evidence strings and nothing else.
 *
 *  Four states, and the difference between the last three is the whole reason
 *  this exists. `fired` is evidence that supports the mapping. `measured` is a
 *  number the scorer took and whose contribution is governed by a floor stated
 *  in the policy, so the figure is shown and no verdict is claimed for it.
 *  `none` is a signal that was computed and found nothing. `absent` is a signal
 *  nobody computed for this pairing -- which is not the same as finding nothing,
 *  and is never drawn as a zero. */
function signalChips(
  candidate: Candidate,
  suggestion: Suggestion,
  policy: ConfidencePolicy | null,
  vetoLexicon: Record<string, string>,
): SignalChip[] {
  const prose = (name: string) => readable(policy?.signals_computed?.[name] ?? "");
  const chips: SignalChip[] = [];

  for (const name of COMPUTED_SIGNALS) {
    const detail = evidenceDetail(candidate.evidence, name);
    const label = SIGNAL_LABEL[name] ?? name;

    if (name === "exact_name_match") {
      if (detail === undefined) {
        chips.push({ name, state: "absent", text: label, title: "Not read for this column." });
      } else if (detail === null) {
        chips.push({ name, state: "fired", text: "Names match", title: prose(name) });
      } else {
        chips.push({ name, state: "none", text: "Names differ", title: prose(name) });
      }
      continue;
    }

    if (name === "semantic_similarity") {
      if (detail === undefined) {
        chips.push({ name, state: "absent", text: label, title: "Not read for this column." });
      } else if (detail === null) {
        chips.push({ name, state: "fired", text: label, title: prose(name) });
      } else {
        // `cos=0.83`. The floor that decides whether it counted is stated in the
        // policy prose and is not on the wire as a number, so the figure is
        // reported and the verdict is left to the sentence in the tooltip.
        const cosine = detail.startsWith("cos=") ? detail.slice(4) : detail;
        chips.push({ name, state: "measured", text: `Name meaning ${cosine}`, title: prose(name) });
      }
      continue;
    }

    if (name === "historical_approvals") {
      if (detail === undefined) {
        chips.push({
          name,
          state: "absent",
          text: label,
          title: "No approval history was consulted for this field.",
        });
      } else if (detail === null) {
        // The signal saturates towards 1 and only drops its detail at full
        // strength. Reachable in principle, and the chip must not read the
        // count out of a string that is not there.
        chips.push({ name, state: "fired", text: label, title: prose(name) });
      } else {
        const count = Number.parseInt(detail, 10);
        if (Number.isFinite(count) && count > 0) {
          chips.push({
            name,
            state: "fired",
            text: `Approved here ${count}×`,
            title: prose(name),
          });
        } else {
          chips.push({
            name,
            state: "none",
            text: "No precedent here",
            title: "This organisation has not approved this column for this field before.",
          });
        }
      }
      continue;
    }

    // type_compatibility. The scorer emits it only when the types agree, so its
    // absence is read from the veto and from the two type fields rather than
    // guessed at.
    if (detail !== undefined) {
      const [observed, declared] = (detail ?? "").split("->");
      chips.push({
        name,
        state: "fired",
        text: declared ? `Type ${observed} fits ${declared}` : label,
        title: prose(name),
      });
    } else if (candidate.vetoes.includes("declared_type_mismatch")) {
      chips.push({
        name,
        state: "none",
        text: `Type ${suggestion.observed_type ?? "?"} ≠ ${suggestion.declared_type ?? "?"}`,
        title: vetoLexicon.declared_type_mismatch ?? prose(name),
      });
    } else {
      chips.push({
        name,
        state: "absent",
        text: "Type not decided",
        title: !suggestion.declared_type
          ? "This field declares no type, so there was nothing to check the column's values against."
          : !suggestion.observed_type
            ? "This column has no values to read a type from."
            : "The type check did not reach a verdict for this column.",
      });
    }
  }

  return chips;
}

const CHIP_STATE: Record<SignalState, string> = {
  fired: "border-ai-confident/30 bg-ai-confident/10 text-ai-confident",
  measured: "border-ai-active/25 bg-ai-active/10 text-ai-active",
  none: "border-border bg-transparent text-muted-foreground",
  absent: "border-dashed border-border bg-transparent text-muted-foreground/70",
};

function SignalStrip({ chips }: { chips: SignalChip[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {chips.map((chip) => (
        <span
          key={chip.name}
          title={chip.title}
          className={cn(
            "rounded border px-1.5 py-0.5 text-[10px] leading-none",
            CHIP_STATE[chip.state],
          )}
        >
          {chip.text}
        </span>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* The confidence badge                                                       */
/* -------------------------------------------------------------------------- */

/** A real score, as a ring that fills and a number that climbs to it.
 *
 *  `strokeDashoffset` rather than a width, so nothing is laid out again while it
 *  fills. Rendered only where a measurement exists -- there is no zero state
 *  here on purpose, because a ring animating to empty says "we looked and found
 *  nothing" about a pairing nobody scored.
 *
 *  `reduced` comes down as a prop, like `Connector`'s does. One of these renders
 *  per field, and `useReducedMotionFlag()` opens a `matchMedia` and subscribes
 *  to it on every call -- so reading it here was one media-query object and one
 *  listener per field on the board. The screen reads it once. */
function ConfidenceBadge({ value, band, reduced }: {
  value: number; band: string; reduced: boolean;
}) {
  const clamped = Math.max(0, Math.min(1, value));
  const percent = Math.round(useCountUp(Math.round(clamped * 100), 700, true));

  const radius = 15;
  const circumference = 2 * Math.PI * radius;
  const line = BAND_LINE[band] ?? BAND_LINE[UNSCORED];

  return (
    <span className="relative flex h-9 w-9 shrink-0 items-center justify-center" title={BAND_MEANING[band] ?? band}>
      <svg viewBox="0 0 36 36" className="h-9 w-9 -rotate-90" aria-hidden>
        <circle cx="18" cy="18" r={radius} fill="none" strokeWidth="2.5" className="stroke-border" />
        <motion.circle
          cx="18"
          cy="18"
          r={radius}
          fill="none"
          strokeWidth="2.5"
          strokeLinecap="round"
          style={{ stroke: line.colour }}
          strokeDasharray={circumference}
          initial={{ strokeDashoffset: reduced ? circumference * (1 - clamped) : circumference }}
          animate={{ strokeDashoffset: circumference * (1 - clamped) }}
          transition={reduced ? { duration: 0 } : { duration: DUR.revealSlow, ease: EASE_OUT }}
        />
      </svg>
      <span className="absolute text-[10px] font-semibold tabular-nums">{percent}</span>
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* The connector overlay                                                      */
/* -------------------------------------------------------------------------- */

type Link = { key: string; fieldId: string; column: string; band: string };
type DrawnLink = Link & { d: string };

/** One curve from a field to its column.
 *
 *  The draw is `pathLength`, which framer-motion renders as a normalised dash
 *  offset -- a stroke growing along its own path, not a box whose width is being
 *  animated. A dashed band cannot be drawn that way (the dash pattern and the
 *  draw pattern are the same attribute), so REVIEW draws solid and then hands
 *  over to a dashed twin, which is also where its slow pulse lives.
 *
 *  Keyed on `field→column` by the caller, so choosing a different column
 *  unmounts this curve -- it fades out where it was -- and mounts a new one that
 *  draws in. */
function Connector({ link, d, index, overridden, dimmed, reduced, visible }: {
  link: Link;
  d: string;
  index: number;
  overridden: boolean;
  dimmed: boolean;
  reduced: boolean;
  visible: boolean;
}) {
  const line = BAND_LINE[link.band] ?? BAND_LINE[UNSCORED];
  // A correction is not part of the arrival sequence: it answers a click that
  // just happened, so it draws immediately and quickly.
  const delay = overridden || reduced ? 0 : staggerDelay(index, 40);
  const draw = reduced ? 0 : overridden ? DUR.base : DUR.revealSlow;
  const pulsing = line.pulse && !reduced && visible;

  // Dimming lives on the group rather than on each path, so pointing at a field
  // fades everything else at micro-interaction speed instead of inheriting the
  // draw's delay -- a hover that answers half a second late reads as lag.
  return (
    <motion.g
      initial={{ opacity: 0 }}
      animate={{ opacity: dimmed ? 0.28 : 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: reduced ? 0 : DUR.base, ease: EASE_OUT }}
    >
      <motion.path
        d={d}
        fill="none"
        strokeLinecap="round"
        style={{ stroke: line.colour }}
        strokeWidth={line.width}
        initial={{ pathLength: reduced ? 1 : 0, opacity: line.opacity }}
        // A dashed band cannot be drawn by pathLength -- the draw and the dash
        // are the same attribute -- so it draws solid and hands over to the
        // dashed twin below the moment the stroke is complete.
        animate={{ pathLength: 1, opacity: line.dashed ? 0 : line.opacity }}
        transition={{
          pathLength: { duration: draw, ease: EASE_OUT, delay },
          opacity: { duration: reduced ? 0 : DUR.base, delay: delay + draw },
        }}
      />
      {line.dashed && (
        <motion.path
          d={d}
          fill="none"
          strokeLinecap="round"
          strokeDasharray="4 5"
          style={{ stroke: line.colour }}
          strokeWidth={line.width}
          initial={{ opacity: reduced ? line.opacity : 0 }}
          animate={{ opacity: pulsing ? PULSE_KEYFRAMES[link.band] : line.opacity }}
          transition={
            pulsing
              ? { duration: 2.4, ease: EASE_IN_OUT, repeat: Infinity, delay: delay + draw }
              : { duration: reduced ? 0 : DUR.base, delay: delay + draw }
          }
        />
      )}
    </motion.g>
  );
}

/* -------------------------------------------------------------------------- */
/* One field on the left rail                                                 */
/* -------------------------------------------------------------------------- */

/** `reduced` is a prop for the same reason `Connector`'s is: one of these
 *  renders per field, and a per-instance `useReducedMotionFlag()` is a
 *  `matchMedia` object and a `change` listener per field. Read once by the
 *  screen, handed down here and on to `ConfidenceBadge`. */
function FieldCard({
  suggestion, chosen, columns, index, policy, vetoLexicon, overridden, active,
  reduced, onBind, onHover, registerRef, onLayoutChange,
}: {
  suggestion: Suggestion;
  chosen: string;
  columns: string[];
  index: number;
  policy: ConfidencePolicy | null;
  vetoLexicon: Record<string, string>;
  overridden: boolean;
  active: boolean;
  reduced: boolean;
  onBind: (fieldId: string, column: string) => void;
  onHover: (fieldId: string | null) => void;
  registerRef: (fieldId: string, el: HTMLElement | null) => void;
  onLayoutChange: () => void;
}) {
  const [open, setOpen] = useState(false);
  const candidate = measuredFor(suggestion, chosen);
  const band = candidate?.band || (chosen ? UNSCORED : "");
  // Every column the scorer ranked for this field, the one it led with
  // included. `alternatives` omits the leader, so listing it alone would leave
  // a reader who corrected a mapping with no way back to the engine's own
  // answer except by remembering its name.
  const scored: Candidate[] = [
    ...(suggestion.column ? [measuredFor(suggestion, suggestion.column)!] : []),
    ...(suggestion.alternatives ?? []),
  ].sort((a, b) => b.score - a.score);
  const others = scored.filter((a) => a.source_ref !== chosen);

  // Whether what is selected is still the engine's own answer. Two things hang
  // off it. The rationale is the server's sentence about the column *it*
  // proposed, so once somebody has chosen a different one it describes a mapping
  // that is no longer on screen and goes away rather than misattributing itself.
  // And the band: a field the engine found no column for carries BLOCK with no
  // pairing behind it, which is a real statement about the field and is worth
  // showing -- but only while nobody has answered it.
  const atServerAnswer = chosen === (suggestion.column ?? "");
  const rationale = atServerAnswer ? suggestion.rationale : "";
  const shownBand = candidate?.band || (atServerAnswer ? suggestion.band : undefined);

  // §13's no-precedent evidence, for a column the reader chose themselves. The
  // leader's own case is already carried by its veto chip, so this only speaks
  // where the correction is. There is no rejection flag anywhere in the
  // response, so this says what the token actually supports and no more.
  const precedent = overridden && candidate ? evidenceDetail(candidate.evidence, "historical_approvals") : undefined;
  const noPrecedent = typeof precedent === "string" && Number.parseInt(precedent, 10) === 0;

  return (
    <motion.div
      ref={(el) => registerRef(suggestion.field_id, el)}
      initial={reduced ? false : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: DUR.reveal, ease: EASE_OUT, delay: staggerDelay(index) }}
      onMouseEnter={() => onHover(suggestion.field_id)}
      onMouseLeave={() => onHover(null)}
      className={cn(
        "relative z-10 rounded-lg border bg-surface p-2.5 transition-colors",
        active ? "border-border-strong" : "border-border",
      )}
    >
      <div className="flex items-start gap-2.5">
        {candidate ? (
          <ConfidenceBadge value={candidate.score} band={band} reduced={reduced} />
        ) : (
          <span
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-dashed border-border text-[10px] text-muted-foreground"
            title={
              chosen
                ? "Chosen by hand. This pairing was not among the ones scored, so there is no score to show."
                : "No column yet, so there is nothing to score."
            }
          >
            —
          </span>
        )}

        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex items-center gap-1.5">
            <span className="min-w-0 flex-1 truncate font-mono text-[11px]" title={suggestion.field_id}>
              {suggestion.field_id}
            </span>
            {suggestion.origin === "condition" && (
              <span
                title="This one is not printed anywhere. It decides which sections are kept."
                className="shrink-0 rounded-md border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground"
              >
                decides a section
              </span>
            )}
            <Pill band={shownBand} />
          </div>

          <select
            value={chosen}
            onChange={(e) => onBind(suggestion.field_id, e.target.value)}
            className="w-full rounded-md border border-border bg-background px-2 py-1 text-xs"
          >
            <option value="">— not mapped —</option>
            {columns.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>

          {chosen && suggestion.sample_value != null && chosen === suggestion.column && (
            <p className="truncate text-[11px] text-muted-foreground" title={suggestion.sample_value}>
              first row: <span className="font-mono text-foreground">{suggestion.sample_value}</span>
            </p>
          )}

          {rationale && (
            <p className="text-[11px] leading-relaxed text-muted-foreground">{plainly(rationale)}</p>
          )}

          {candidate && (
            <SignalStrip chips={signalChips(candidate, suggestion, policy, vetoLexicon)} />
          )}

          {candidate?.vetoes.map((code) => (
            <p key={code} className="flex items-start gap-1.5 text-[11px] leading-relaxed text-ai-uncertain">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
              <span>{vetoLexicon[code] ?? <span className="font-mono">{code}</span>}</span>
            </p>
          ))}

          {noPrecedent && (
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              No precedent — this organisation has not approved this column for this field before.
            </p>
          )}

          {chosen && !candidate && (
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              Chosen by hand. This column was not one of the ones scored for this field, so nothing
              was measured about the pairing.
            </p>
          )}

          {others.length > 0 && (
            <div>
              <button
                type="button"
                onClick={() => { setOpen((v) => !v); onLayoutChange(); }}
                className="flex items-center gap-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
              >
                <ChevronDown className={cn("h-3 w-3 transition-transform", open && "rotate-180")} />
                {others.length} other column{others.length === 1 ? "" : "s"} scored for this field
              </button>
              {open && (
                <ul className="mt-1 space-y-1">
                  {others.map((alt) => (
                    <li key={alt.source_ref}>
                      <button
                        type="button"
                        onClick={() => onBind(suggestion.field_id, alt.source_ref)}
                        className="flex w-full items-center gap-2 rounded-md border border-border px-2 py-1 text-left text-[11px] transition-colors hover:border-border-strong"
                      >
                        <span className="min-w-0 flex-1 truncate font-mono">{alt.source_ref}</span>
                        <span className="tabular-nums text-muted-foreground">
                          {Math.round(alt.score * 100)}
                        </span>
                        <Pill band={alt.band} />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
}

/* -------------------------------------------------------------------------- */
/* Unmatched branch values                                                    */
/* -------------------------------------------------------------------------- */

/** The most expensive thing on this screen to walk past.
 *
 *  Every row carrying one of these produces a letter with a conditional section
 *  missing, and nothing downstream says so until a QA note after the batch. So
 *  it shakes once on arrival and then holds a slow pulse until somebody either
 *  maps every value or says they have read it. Both the shake and the pulse are
 *  off under reduced motion -- the panel is still the loudest thing here on
 *  colour and position alone. */
/** Module-level so the keyframes keep one identity across renders. A fresh
 *  array every render is a fresh target, and the panel would shake again every
 *  time somebody touched one of its selects -- which is the opposite of "once". */
const SHAKE_ONCE = { x: [0, -7, 7, -5, 5, 0] };
const NO_SHAKE = { x: 0 };

function UnmatchedPanel({
  unmatched, valueMap, unmappedCount, acknowledged, onAcknowledge, onMap,
}: {
  unmatched: UnmatchedValue[];
  valueMap: Record<string, Record<string, string>>;
  unmappedCount: number;
  acknowledged: boolean;
  onAcknowledge: () => void;
  onMap: (fieldId: string, observed: string, chosen: string) => void;
}) {
  const reduced = useReducedMotionFlag();
  const visible = usePageVisible();

  return (
    <motion.div
      initial={{ x: 0 }}
      animate={reduced ? NO_SHAKE : SHAKE_ONCE}
      transition={{ duration: 0.42, ease: EASE_IN_OUT }}
      className={cn(
        "ml-10 space-y-2 rounded-lg border border-ai-uncertain/40 bg-ai-uncertain/10 p-3",
        !acknowledged && !reduced && visible && "ai-pulse",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex flex-1 items-center gap-1.5 text-xs font-medium text-ai-uncertain">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          {unmatched.length} spreadsheet value{unmatched.length === 1 ? "" : "s"} match no branch of this template
        </div>
        {!acknowledged && (
          <button
            type="button"
            onClick={onAcknowledge}
            className="shrink-0 rounded-md border border-ai-uncertain/40 px-2 py-0.5 text-[11px] text-ai-uncertain transition-colors hover:bg-ai-uncertain/10"
          >
            I have read this
          </button>
        )}
      </div>
      <p className="text-[11px] text-muted-foreground">
        Every row carrying one of these generates with its conditional section silently missing.
        Point each value at the branch it means — the answer applies to every row that carries it.
      </p>
      {unmatched.map((u) => (
        <div key={`${u.field_id}|${u.observed_value}`} className="flex flex-wrap items-center gap-2">
          <span className="text-[11px]" title={`${u.field_id}${u.column ? ` · column ${u.column}` : ""}`}>
            <span className="font-mono">{u.column ?? u.field_id}</span> = “{u.observed_value}”
          </span>
          <ArrowRight className="h-3 w-3 shrink-0 text-muted-foreground" />
          <select
            value={valueMap[u.field_id]?.[u.observed_value] ?? ""}
            onChange={(e) => onMap(u.field_id, u.observed_value, e.target.value)}
            className="min-w-[10rem] rounded-md border border-border bg-background px-2 py-1 text-xs"
          >
            <option value="">— leave unmapped —</option>
            {u.offered.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
          <span className="text-[11px] text-muted-foreground">
            offered: {u.offered.join(", ")}
          </span>
        </div>
      ))}
      {unmappedCount > 0 && (
        <div className="text-[11px] font-medium text-ai-uncertain">
          {unmappedCount} still unmapped — those rows will generate with the section missing.
        </div>
      )}
    </motion.div>
  );
}

/* -------------------------------------------------------------------------- */
/* What the score is made of                                                  */
/* -------------------------------------------------------------------------- */

/** The provenance panel, straight off the response.
 *
 *  This is what replaced the agent trace somebody will ask for. There is no
 *  progress feed on `binding-suggestions` and no stages are published for it, so
 *  narrating four invented steps would be a lie told at 320ms a frame. What is
 *  real is `confidence_policy`: which signals were combined, which were not and
 *  why, what the arithmetic can reach, and that the weights behind it have not
 *  been fitted to anything yet. It tells the same story and every line of it is
 *  on the wire. */
function PolicyPanel({ policy }: { policy: ConfidencePolicy }) {
  const computed = Object.entries(policy.signals_computed ?? {});
  const missing = Object.entries(policy.signals_not_computed ?? {});
  const ceiling = policy.max_attainable_score;
  const floor = policy.bands?.auto_accept;

  return (
    <FadeIn className="ml-10">
      <section className="space-y-3 rounded-lg border border-border bg-background/40 p-3">
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          No model is asked for any of this. Each score below is arithmetic over the signals the
          engine could measure, and evidence only ever adds — a second weak signal raises a score
          and can never lower it.
        </p>

        {computed.length > 0 && (
          <ul className="space-y-1.5">
            {computed.map(([name, prose]) => (
              <li key={name} className="text-[11px] leading-relaxed">
                <span className="font-medium text-ai-confident">{SIGNAL_LABEL[name] ?? name}</span>
                <span className="text-muted-foreground"> — {readable(prose)}</span>
              </li>
            ))}
          </ul>
        )}

        {missing.length > 0 && (
          <div className="space-y-1.5 border-t border-border pt-2.5">
            <p className="text-[11px] font-medium text-muted-foreground">Not measured, and why</p>
            <ul className="space-y-1.5">
              {missing.map(([name, prose]) => (
                <li key={name} className="text-[11px] leading-relaxed text-muted-foreground/80">
                  <span className="font-medium">{SIGNAL_LABEL[name] ?? name}</span> — {readable(prose)}
                </li>
              ))}
            </ul>
          </div>
        )}

        {(ceiling != null || floor != null || policy.weights_calibrated === false) && (
          <div className="space-y-1 border-t border-border pt-2.5 text-[11px] leading-relaxed text-muted-foreground">
            {ceiling != null && floor != null && (
              <p>
                The highest score reachable with the signals above is{" "}
                <span className="tabular-nums text-foreground">{Math.round(ceiling * 100)}</span>, and
                the top band starts at{" "}
                <span className="tabular-nums text-foreground">{Math.round(floor * 100)}</span>
                {policy.auto_accept_reachable === false && " — so nothing reaches it today"}.
              </p>
            )}
            {policy.weights_calibrated === false && (
              <p>
                The weights behind these signals are starting values. They have not been fitted to
                measured reviewer decisions yet, so treat a score as a ranking, not a probability.
              </p>
            )}
          </div>
        )}
      </section>
    </FadeIn>
  );
}

/* -------------------------------------------------------------------------- */

export function DocumentMapping({ project, watch, onGenerating }: {
  project: any;
  /** The batch this project is running, held by the project screen so it
   *  survives leaving this stage. */
  watch: BatchWatch;
  /** Called when a batch starts, so the screen can move to Documents -- which is
   *  where its output is going to appear. */
  onGenerating: () => void;
}) {
  const templates: any[] = project?.templates ?? [];
  const sources: any[] = project?.sources ?? [];

  const [templateId, setTemplateId] = useState<string>(templates[0]?.id ?? "");
  const [sourceId, setSourceId] = useState<string>(sources[0]?.id ?? "");

  const source = sources.find((s: any) => s.id === sourceId);
  const sourceVersionId: string = source?.currentVersionId ?? "";
  const sourceType: string = source?.type ?? "";

  const [sheets, setSheets] = useState<string[]>([]);
  const [sheet, setSheet] = useState<string>("");
  const [sheetsError, setSheetsError] = useState<string | null>(null);

  const [manifest, setManifest] = useState<any>(null);
  // True from the first render rather than from the effect: effects run after
  // paint, so starting at false showed "this template has not been read yet" for
  // one frame on every visit -- a flat contradiction of what usually lands a
  // moment later.
  const [manifestLoading, setManifestLoading] = useState(!!templateId);
  const [manifestError, setManifestError] = useState<string | null>(null);
  // Bumped by the error banner's Retry. The lookup is a GET with no side
  // effects, so re-running the effect is the whole of "try again".
  const [lookupNonce, setLookupNonce] = useState(0);
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [suggestError, setSuggestError] = useState<string | null>(null);
  const [columns, setColumns] = useState<string[]>([]);
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [unmatched, setUnmatched] = useState<UnmatchedValue[]>([]);
  const [valueMap, setValueMap] = useState<Record<string, Record<string, string>>>({});
  // The two halves of the response the previous screen threw away: how the
  // engine says its own numbers were made, and how the bands fell when the
  // spreadsheet was read.
  const [policy, setPolicy] = useState<ConfidencePolicy | null>(null);
  const [bandSummary, setBandSummary] = useState<Record<string, number> | null>(null);
  const [policyOpen, setPolicyOpen] = useState(false);
  // Fields the reader has re-pointed themselves. Only used for presentation: a
  // correction draws immediately instead of taking its turn in the arrival
  // stagger, and it is where the no-precedent note speaks.
  const [overridden, setOverridden] = useState<Record<string, true>>({});
  const [unmatchedAck, setUnmatchedAck] = useState(false);
  const [hovered, setHovered] = useState<string | null>(null);

  const [busy, setBusy] = useState<string | null>(null);
  // Two steps, where there were three. The first was "review and approve the
  // manifest", which reading a template now does for itself wherever that is
  // allowed -- so what is left is the work this screen is actually for: point the
  // template's fields at the spreadsheet's columns, then run the batch.
  const step: Step = !watch.job ? 1 : 2;

  const reduced = useReducedMotionFlag();
  const pageVisible = usePageVisible();

  // Suggestions, bindings and the value map are all derived from one
  // (template, source, sheet) triple. Left on screen after that triple
  // changes they are worse than stale: generate() posts `bindings` -- the
  // previous spreadsheet's column names -- against the new source_version_id,
  // and writes whatever happens to sit under those headers into every letter.
  const resetDerived = useCallback(() => {
    setSuggestions([]);
    setSuggestError(null);
    setColumns([]);
    setBindings({});
    setUnmatched([]);
    setValueMap({});
    setPolicy(null);
    setBandSummary(null);
    setOverridden({});
    setUnmatchedAck(false);
    // The running batch is deliberately *not* cleared here. It belongs to the
    // project, not to this (template, source, sheet) triple, and dropping it on
    // a dropdown change was how a finished batch's archive became unreachable.
  }, []);

  // An existing manifest means this template has been here before; picking it up
  // rather than re-compiling avoids paying for a model call to learn what is
  // already known.
  useEffect(() => {
    if (!templateId) return;
    setManifest(null);
    setManifestError(null);
    setManifestLoading(true);
    resetDerived();
    // Guarded like the sheets effect below. Switching template A -> B while A's
    // request is in flight let A's response land after B's reset, so the screen
    // showed A's manifest while `templateId` was B: Approve would approve A, and
    // step 3 would bind B's spreadsheet against A's fields.
    let live = true;
    api.listManifests(templateId)
      .then((r) => {
        if (!live) return;
        const latest = (r.items ?? [])[0];
        if (latest) setManifest(latest);
      })
      // A template with nothing compiled answers 200 with an empty list, so a
      // rejection here is a real failure, not a first visit. It is shown in
      // place rather than as a toast: a toast that has faded leaves a screen
      // saying this template has never been read, which is a different and
      // worse claim than "we could not find out".
      .catch((e: any) => {
        if (!live) return;
        setManifestError(e?.message ?? String(e));
      })
      .finally(() => {
        if (live) setManifestLoading(false);
      });
    return () => { live = false; };
  }, [templateId, lookupNonce, resetDerived]);

  // Which sheet a workbook is read from is a choice, and until now it was made
  // silently by the backend falling through to worksheets[0]. The names come
  // from the records endpoint, which returns them for xlsx only.
  useEffect(() => {
    resetDerived();
    setSheets([]); setSheet(""); setSheetsError(null);
    if (!sourceVersionId || sourceType !== "xlsx") return;
    let live = true;
    api.sourceRecords(sourceVersionId, 1)
      .then((r) => {
        if (!live) return;
        const names = r.sheets ?? [];
        setSheets(names);
        setSheet(names[0] ?? "");
      })
      .catch((e: any) => {
        if (!live) return;
        // Not fatal -- mapping still runs against the default sheet -- but a
        // workbook whose tabs could not be listed must say so rather than look
        // like a single-sheet file.
        setSheetsError(e?.message ?? String(e));
      });
    return () => { live = false; };
  }, [sourceId, sourceVersionId, sourceType, resetDerived]);

  // There was an approval block here: `refreshValidation`, `approve` and
  // `acknowledgeWarning`, plus the dialog that made you type a note for each
  // compiler warning before the manifest could be signed. All of it existed to
  // satisfy one rule -- generation refused a manifest nobody had approved -- and
  // that rule is gone for ordinary templates. `GET .../validation`,
  // `:approve` and `warnings:resolve` are all still served, and a template
  // flagged legally binding still needs them; this screen simply is not where
  // that happens.

  async function loadSuggestions() {
    if (!manifest?.id) return;
    if (!sources.length) {
      toast.error("No spreadsheet yet", {
        description: "Download the data template above, fill it in, and upload it as a source.",
      });
      return;
    }
    if (!sourceVersionId) { toast.error("That upload has not finished processing yet."); return; }
    setBusy("suggest");
    setSuggestError(null);
    try {
      const r = await api.bindingSuggestions(manifest.id, sourceVersionId, sheet || undefined);
      // `confidence_policy` and `band_summary` are on every reply from this
      // endpoint; the shared client types the response down to the keys the old
      // screen read. Widened here rather than in the client, which four other
      // screens import and none of them wants this shape.
      const full = r as typeof r & {
        confidence_policy?: ConfidencePolicy;
        band_summary?: Record<string, number>;
      };
      setSuggestions((full.suggestions ?? []) as Suggestion[]);
      setColumns(full.columns ?? []);
      const seed: Record<string, string> = {};
      for (const s of full.suggestions ?? []) if (s.column) seed[s.field_id] = s.column;
      setBindings(seed);
      setUnmatched(full.unmatched_condition_values ?? []);
      setValueMap({});
      setPolicy(full.confidence_policy ?? null);
      setBandSummary(full.band_summary ?? null);
      setOverridden({});
      setUnmatchedAck(false);
    } catch (e: any) {
      // Banner rather than toast, because the fix is the button it carries and
      // a toast takes the retry away with it when it fades.
      setSuggestError(e?.message ?? String(e));
    } finally { setBusy(null); }
  }

  async function generate() {
    if (!manifest?.id) return;
    if (!sources.length) {
      toast.error("No spreadsheet yet", {
        description: "Download the data template above, fill it in, and upload it as a source.",
      });
      return;
    }
    if (!sourceVersionId) { toast.error("That upload has not finished processing yet."); return; }
    setBusy("generate");
    try {
      const field_bindings = Object.fromEntries(Object.entries(bindings).filter(([, column]) => !!column));
      await api.saveBinding(manifest.id, {
        source_version_id: sourceVersionId,
        field_bindings,
        // Vocabulary the reviewer reconciled in step 3. Without it every row
        // carrying an unmatched value generates with its conditional section
        // missing, and nothing says so until a QA note after the batch.
        ...(Object.keys(valueMap).length ? { value_map: valueMap } : {}),
      });
      const started = await api.generateBatch(manifest.id, { source_version_id: sourceVersionId, language: "en" });
      watch.start({ job_id: started.job_id, status: started.status });
      toast.success("Generating…", {
        description: "Watch it on the Documents stage — that is where the letters appear.",
      });
      // Moved for the user, not away from them. The output of this is documents,
      // and the batch panel now lives beside them; leaving the reader here would
      // leave them watching a progress bar on a screen about column mappings.
      onGenerating();
    } catch (e: any) {
      toast.error("Could not start generation", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setBusy(null);
    }
  }

  async function downloadSourceTemplate() {
    if (!manifest?.id) return;
    setBusy("srctemplate");
    try {
      const { url, filename } = await api.sourceTemplate(manifest.id);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      // Appended before clicking and revoked on the next tick: a detached
      // anchor's click() does nothing in Firefox, and revoking synchronously
      // can cancel the download before the browser has read the blob.
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      toast.success("Data template downloaded", {
        description: "Fill it in and upload it as a source — the columns already match this template.",
      });
    } catch (e: any) {
      toast.error("Could not build the data template", { description: plainly(e?.message ?? String(e)) });
    } finally { setBusy(null); }
  }

  /* ---------------------------------------------------------------- geometry */

  const boardRef = useRef<HTMLDivElement | null>(null);
  const leftRailRef = useRef<HTMLDivElement | null>(null);
  const rightRailRef = useRef<HTMLDivElement | null>(null);
  const fieldEls = useRef(new Map<string, HTMLElement>());
  const columnEls = useRef(new Map<string, HTMLElement>());
  const [drawn, setDrawn] = useState<DrawnLink[]>([]);
  // What was last committed to state, so a rAF that measured the same geometry
  // does not re-render the whole overlay. Scroll fires a lot.
  const lastGeometry = useRef("");
  const [layoutNonce, setLayoutNonce] = useState(0);

  const registerField = useCallback((fieldId: string, el: HTMLElement | null) => {
    if (el) fieldEls.current.set(fieldId, el);
    else fieldEls.current.delete(fieldId);
  }, []);

  const registerColumn = useCallback((column: string, el: HTMLElement | null) => {
    if (el) columnEls.current.set(column, el);
    else columnEls.current.delete(column);
  }, []);

  /** Which curves exist, and what band each one is in. The band comes from the
   *  candidate that matches the column now selected -- so correcting a mapping
   *  to a column the scorer did rank restyles the line to that candidate's real
   *  band, and correcting it to one nobody scored draws the idle line instead of
   *  keeping the band the old column earned. */
  const links: Link[] = useMemo(() => {
    const out: Link[] = [];
    for (const s of suggestions) {
      const column = bindings[s.field_id];
      if (!column) continue;
      const candidate = measuredFor(s, column);
      out.push({
        key: `${s.field_id}→${column}`,
        fieldId: s.field_id,
        column,
        band: candidate?.band || UNSCORED,
      });
    }
    return out;
  }, [suggestions, bindings]);

  const measure = useCallback(() => {
    const board = boardRef.current;
    if (!board) return;
    const box = board.getBoundingClientRect();
    const next: DrawnLink[] = [];
    for (const link of links) {
      const from = fieldEls.current.get(link.fieldId);
      const to = columnEls.current.get(link.column);
      if (!from || !to) continue;
      const a = from.getBoundingClientRect();
      const b = to.getBoundingClientRect();
      // Narrow viewports stack the two rails, and a curve from the bottom of one
      // card to the top of a list below it does not mean anything. Nothing is
      // drawn rather than something incoherent being drawn.
      if (b.left < a.right + 12) continue;
      const x1 = a.right - box.left;
      // Anchored near the top of the card rather than at its middle: a card
      // grows downwards when its other candidates are opened, and a line tied to
      // the centre would slide half a card's height away from the field name it
      // belongs to. 28px is where the score ring sits.
      const y1 = a.top + Math.min(a.height / 2, 28) - box.top;
      const x2 = b.left - box.left;
      const y2 = b.top + b.height / 2 - box.top;
      const bend = Math.max(18, (x2 - x1) * 0.45);
      next.push({
        ...link,
        d: `M${x1.toFixed(1)},${y1.toFixed(1)} C${(x1 + bend).toFixed(1)},${y1.toFixed(1)} ${(x2 - bend).toFixed(1)},${y2.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}`,
      });
    }
    const serialised = JSON.stringify(next);
    if (serialised === lastGeometry.current) return;
    lastGeometry.current = serialised;
    setDrawn(next);
  }, [links]);

  // Measured after layout and before paint, so a curve is never drawn against
  // last frame's positions. Re-measured on anything that can move an endpoint:
  // the board resizing, either rail changing height (a card opening its
  // alternatives), the window resizing, and any ancestor scrolling -- all
  // throttled to one rAF, because scroll fires far faster than the geometry
  // actually changes.
  useLayoutEffect(() => {
    measure();
    const board = boardRef.current;
    if (!board || typeof ResizeObserver === "undefined") return;
    let raf = 0;
    const schedule = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => { raf = 0; measure(); });
    };
    const observer = new ResizeObserver(schedule);
    observer.observe(board);
    if (leftRailRef.current) observer.observe(leftRailRef.current);
    if (rightRailRef.current) observer.observe(rightRailRef.current);
    window.addEventListener("resize", schedule);
    window.addEventListener("scroll", schedule, true);
    // The rails arrive on a staggered six-pixel lift, and a card mid-transform
    // measures six pixels off its resting position. Nothing observes the end of
    // a transform, so the board is re-measured once the arrival is over -- a
    // no-op unless something actually moved, because `measure` only commits
    // geometry that differs from what is already drawn.
    const settle = [420, 820].map((ms) => setTimeout(schedule, ms));
    return () => {
      if (raf) cancelAnimationFrame(raf);
      observer.disconnect();
      settle.forEach(clearTimeout);
      window.removeEventListener("resize", schedule);
      window.removeEventListener("scroll", schedule, true);
    };
  }, [measure, layoutNonce]);

  const bumpLayout = useCallback(() => setLayoutNonce((n) => n + 1), []);

  const bind = useCallback((fieldId: string, column: string) => {
    setBindings((prev) => ({ ...prev, [fieldId]: column }));
    setOverridden((prev) => ({ ...prev, [fieldId]: true }));
  }, []);

  /* ------------------------------------------------------------------ render */

  // Only a template is required to get in. This used to demand a spreadsheet as
  // well, which made "Download data template" unreachable by anyone who did not
  // already have one -- and not having one is the entire reason to want it.
  // Compiling needs the template alone; the source is what steps 3 and 4 need.
  if (!templates.length) {
    return (
      <PolishedEmpty
        icon={<Table2 className="h-6 w-6" />}
        title="Upload a template first"
        subtitle="Document Mapping reads the template to work out what data it needs. Once it has, it can hand you a spreadsheet with the right columns already in it."
      />
    );
  }

  const conditions: any[] = manifest?.conditions ?? [];

  // Read straight off the manifest. This used to prefer `GET .../validation`,
  // which returned the same warnings alongside the approval blockers derived
  // from them -- and there are no approval blockers on this screen any more, so
  // the extra request bought a second copy of what the manifest already carries.
  //
  // The dispositions are gone with it. An acknowledgement was how a warning
  // stopped blocking approval; nothing blocks now, so a warning is either worth
  // reading or it is not, and there is nothing to record against it here.
  const warningGroups = groupWarnings((manifest?.warnings ?? []) as ManifestWarning[]);

  const mappedCount = Object.values(bindings).filter(Boolean).length;
  const unmappedValueCount = unmatched.filter((u) => !valueMap[u.field_id]?.[u.observed_value]).length;
  const unmatchedAcknowledged = unmatchedAck || unmappedValueCount === 0;
  // The client cannot pass `sheet` to generate-batch, so a batch always reads
  // the workbook's first sheet. Mapping against another one and then generating
  // would fill this sheet's headers from that sheet's rows.
  const sheetBlocksGeneration = sheets.length > 1 && !!sheet && sheet !== sheets[0];

  // Veto explanations arrive attached to the leading candidate only; every
  // alternative carries the bare code. The explanation for a code is the same
  // wherever it fires, so one pass over the response builds the lexicon the
  // whole board reads from -- and a code nobody explained is shown verbatim
  // rather than paraphrased, because that is the string somebody quotes.
  const vetoLexicon: Record<string, string> = {};
  for (const s of suggestions) {
    for (const v of s.vetoes ?? []) {
      if (v?.code && v.explanation && !vetoLexicon[v.code]) vetoLexicon[v.code] = v.explanation;
    }
  }

  const boundColumns = new Set(Object.values(bindings).filter(Boolean));
  const columnBand: Record<string, string> = {};
  for (const link of links) columnBand[link.column] = link.band;

  const autoAcceptTitle =
    policy?.auto_accept_reachable === false && policy.max_attainable_score != null && policy.bands?.auto_accept != null
      ? `Empty by arithmetic, not by chance: with the signals measured today the highest score reachable is ${Math.round(policy.max_attainable_score * 100)}, and this band starts at ${Math.round(policy.bands.auto_accept * 100)}.`
      : BAND_MEANING.AUTO_ACCEPT;

  return (
    <div className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1.5">
          <span className="text-xs font-medium text-muted-foreground">Template</span>
          <select
            value={templateId} onChange={(e) => setTemplateId(e.target.value)} disabled={!!busy}
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm disabled:opacity-50"
          >
            {templates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </label>
        <label className="space-y-1.5">
          <span className="text-xs font-medium text-muted-foreground">Source spreadsheet</span>
          <select
            value={sourceId} onChange={(e) => setSourceId(e.target.value)} disabled={!!busy || !sources.length}
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm disabled:opacity-50"
          >
            {sources.length
              ? sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)
              : <option value="">None yet — download the data template below, fill it in, then upload it</option>}
          </select>
        </label>
      </div>

      {sheets.length > 1 && (
        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-muted-foreground">
            Sheet <span className="font-normal">({sheets.length} in this workbook)</span>
          </span>
          <select
            value={sheet}
            onChange={(e) => { setSheet(e.target.value); resetDerived(); }}
            disabled={!!busy}
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm disabled:opacity-50 sm:w-1/2"
          >
            {sheets.map((s, i) => <option key={s} value={s}>{i === 0 ? `${s} (default)` : s}</option>)}
          </select>
        </label>
      )}

      {sheetsError && (
        <ErrorBanner
          title="Could not list the sheets in this workbook"
          message="The first sheet will be used, which is the one generating reads anyway. Everything below still works."
          detail={plainly(sheetsError)}
        />
      )}

      {/* Reading a template is a fact about the template, so it happens once,
          at upload, on the Template stage. There is no Compile button here and
          no Re-compile either: the second was destructive in a way its label did
          not suggest -- it threw away the approval, the column mappings, the
          branch value map and the job handle, and `job.job_id` was the only
          route back to a finished batch's ZIP. Reading a template again is done
          from the template's own row, where the consequence is visible.

          "Compile & self-verify" is gone for a different reason: it had already
          stopped being a second thing. The endpoint's signature is
          `(template_file_id, progress_token, db, user)` and FastAPI drops the
          `agentic` flag the button sent, so both buttons made the identical
          request and only the spinner's label differed. */}
      {manifestError ? (
        <ErrorBanner
          title="Could not check this template"
          message="We could not tell whether this template has been read yet. Nothing has changed — try again."
          detail={plainly(manifestError)}
          onRetry={() => setLookupNonce((n) => n + 1)}
          retrying={manifestLoading}
        />
      ) : manifestLoading ? (
        /* The same panel, the same line of figures, the same button-sized block:
           when the real summary lands it takes this one's place without moving
           the two steps below it down the page. */
        <section
          role="status"
          aria-label="Checking what this template needs"
          className="rounded-xl border border-border bg-background/40 p-4 space-y-3"
        >
          <SkeletonBar className="h-3 w-72 max-w-full" />
          <div className="flex items-center gap-3">
            <SkeletonBar className="h-8 w-44 shrink-0 rounded-lg" />
            <SkeletonBar className="h-3 w-full" />
          </div>
        </section>
      ) : !manifest ? (
        <PolishedEmpty
          icon={<Wand2 className="h-6 w-6" />}
          title="This template has not been read yet"
          subtitle="Reading it is what works out which columns your spreadsheet needs. Go back to the Template stage and read it — the row there says whether it failed and why."
        />
      ) : (
        <FadeIn>
          <section className="rounded-xl border border-border bg-background/40 p-4 space-y-3">
            <ReadingSummary
              fields={(manifest.fields ?? []).length}
              conditions={conditions.length}
              confidence={manifest.confidence ?? null}
              readBy={manifest.compiled_by}
            />
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" onClick={downloadSourceTemplate} disabled={!!busy}>
                {busy === "srctemplate" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                Download data template
              </Button>
              <span className="text-xs text-muted-foreground">
                A spreadsheet with this template&rsquo;s columns already named, and a fixed list of
                choices on every column that decides which sections are kept.
              </span>
            </div>
          </section>
        </FadeIn>
      )}

      {/* What the compiler was unsure about, as advice rather than a gate.
          These used to be a wall: each one had to be acknowledged with a written
          note before the manifest could be approved, and generation refused an
          unapproved manifest -- so a template with three warnings was three
          dialogs and a signature away from producing anything.

          They are still worth reading, because they predict a specific failure:
          an unclaimed placeholder means every document generated from this
          template fails its QA check with "Leftover placeholder brackets", and
          finding that out here beats finding it out after a batch. But they
          describe a risk, and the person who can judge it is the one about to
          press Generate -- so they are shown next to that button, not in front
          of it.

          The text comes from the compiler and is written for whoever wrote the
          rules, so it goes through `plainly()` on the way to the screen. The
          code beside it does not: it is the string somebody quotes in a support
          thread, and paraphrasing that is how a report stops being findable. */}
      {manifest && warningGroups.length > 0 && (
        <FadeIn>
          <section className="rounded-xl border border-ai-uncertain/25 bg-ai-uncertain/5 p-4">
            <p className="flex items-center gap-1.5 text-sm font-medium text-ai-uncertain">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              {warningGroups.length} thing{warningGroups.length === 1 ? "" : "s"} the engine was unsure about
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              None of these stop you generating. They are the places a document is most likely to come
              back with a QA failure, so they are worth a look first.
            </p>
            <ul className="mt-2 space-y-1">
              {warningGroups.slice(0, 4).map((g) => (
                <li key={g.code} className="text-xs text-muted-foreground">
                  <span className="font-mono text-[10px] uppercase opacity-70">{g.code}</span>{" "}
                  {plainly(g.message || g.occurrences[0]?.detail || "")}
                  {g.occurrences.length > 1 && (
                    <span className="opacity-70"> · {g.occurrences.length} places</span>
                  )}
                </li>
              ))}
              {warningGroups.length > 4 && (
                <li className="text-xs text-muted-foreground opacity-70">
                  and {warningGroups.length - 4} more
                </li>
              )}
            </ul>
          </section>
        </FadeIn>
      )}

      {/* 1 — map fields to columns */}
      {manifest && (
        <section className="rounded-xl border border-border bg-background/40 p-4 space-y-3">
          <StepHeader step={1} active={step === 1} done={mappedCount > 0}
            title="Map fields to columns" hint="This is the step that connects the spreadsheet to the letter" />

          {!sources.length && (
            <div className="ml-10 rounded-lg border border-dashed border-border p-3 text-xs text-muted-foreground">
              No spreadsheet on this project yet. Use <span className="font-medium text-foreground">Download data
              template</span> above — it already has this template&rsquo;s columns and a fixed list of choices
              on the ones that decide which sections are kept — then upload it under Sources.
            </div>
          )}
          <div className="pl-10">
            <Button size="sm" variant="outline" onClick={loadSuggestions} disabled={!!busy || !sources.length}>
              {busy === "suggest" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              {suggestions.length ? "Re-read spreadsheet" : "Match columns"}
            </Button>
            {sheets.length > 1 && (
              <span className="ml-2 text-[11px] text-muted-foreground">reading “{sheet}”</span>
            )}
          </div>

          {suggestError && (
            <ErrorBanner
              className="ml-10"
              title="Could not read the spreadsheet"
              message="Nothing was saved and nothing was mapped. Check the file is still there, then try again."
              detail={plainly(suggestError)}
              onRetry={() => void loadSuggestions()}
              retrying={busy === "suggest"}
            />
          )}

          {/* Rows whose condition value selects no branch. Each is a letter that
              would generate with a section missing, and here it still costs one
              line to fix. */}
          {unmatched.length > 0 && (
            <UnmatchedPanel
              unmatched={unmatched}
              valueMap={valueMap}
              unmappedCount={unmappedValueCount}
              acknowledged={unmatchedAcknowledged}
              onAcknowledge={() => setUnmatchedAck(true)}
              onMap={(fieldId, observed, chosen) => {
                setValueMap((prev) => {
                  const forField = { ...(prev[fieldId] ?? {}) };
                  if (chosen) forField[observed] = chosen;
                  else delete forField[observed];
                  const next = { ...prev, [fieldId]: forField };
                  if (!Object.keys(forField).length) delete next[fieldId];
                  return next;
                });
              }}
            />
          )}

          {/* The board's own geometry, held while the request is in flight: four
              field cards on the left and a column list on the right, so nothing
              moves when the real ones take their place. There is no progress
              feed on this request, so there is nothing here pretending to
              narrate one. */}
          {busy === "suggest" && !suggestions.length && (
            <div
              role="status"
              aria-label="Reading the spreadsheet"
              className="ml-10 grid gap-x-12 gap-y-2 md:grid-cols-[minmax(0,1fr)_minmax(0,15rem)]"
            >
              <div className="space-y-2">
                {Array.from({ length: 4 }).map((_, i) => (
                  <div key={i} className="flex items-start gap-2.5 rounded-lg border border-border p-2.5">
                    <SkeletonBar className="h-9 w-9 shrink-0 rounded-full" />
                    <div className="flex-1 space-y-1.5">
                      <SkeletonBar className="h-3 w-40" />
                      <SkeletonBar className="h-6 w-full rounded-md" />
                      <SkeletonBar className="h-2.5 w-3/4" />
                    </div>
                  </div>
                ))}
              </div>
              <div className="space-y-1.5">
                {Array.from({ length: 7 }).map((_, i) => (
                  <SkeletonBar key={i} className="h-6 rounded-md" />
                ))}
              </div>
            </div>
          )}

          {suggestions.length > 0 && (
            <>
              {/* The legend is the band summary the server sent when it read the
                  spreadsheet, not a live recount -- correcting a mapping below
                  changes that row's band and not this line, and the label says
                  which of the two it is. */}
              <div className="ml-10 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                {bandSummary && BAND_ORDER.map((band) => (
                  <span
                    key={band}
                    title={band === "AUTO_ACCEPT" ? autoAcceptTitle : BAND_MEANING[band]}
                    className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground"
                  >
                    <span
                      aria-hidden
                      className="h-2 w-2 rounded-full"
                      style={{ background: BAND_LINE[band].colour, opacity: BAND_LINE[band].opacity }}
                    />
                    <span className="tabular-nums text-foreground">{bandSummary[band] ?? 0}</span>
                    {band.replace("_", " ").toLowerCase()}
                  </span>
                ))}
                {bandSummary && (
                  <span className="text-[11px] text-muted-foreground/70">when the spreadsheet was read</span>
                )}
                {policy && (
                  <button
                    type="button"
                    onClick={() => setPolicyOpen((v) => !v)}
                    className="ml-auto flex items-center gap-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
                  >
                    <ChevronDown className={cn("h-3 w-3 transition-transform", policyOpen && "rotate-180")} />
                    What this score is made of
                  </button>
                )}
              </div>

              {policy && policyOpen && <PolicyPanel policy={policy} />}

              <div
                ref={boardRef}
                className="relative ml-10 grid gap-x-12 gap-y-3 md:grid-cols-[minmax(0,1fr)_minmax(0,15rem)]"
              >
                {/* One overlay for every curve. Absolutely positioned over the
                    board and inert to the pointer, so the selects underneath
                    still take every click. Its user units are the board's CSS
                    pixels, which is why the paths are measured in them. */}
                <svg
                  aria-hidden
                  className="pointer-events-none absolute inset-0 z-0 h-full w-full"
                >
                  <AnimatePresence>
                    {drawn.map((link, i) => (
                      <Connector
                        key={link.key}
                        link={link}
                        d={link.d}
                        index={i}
                        overridden={!!overridden[link.fieldId]}
                        dimmed={!!hovered && hovered !== link.fieldId}
                        reduced={reduced}
                        visible={pageVisible}
                      />
                    ))}
                  </AnimatePresence>
                </svg>

                <div ref={leftRailRef} className="space-y-2">
                  <p className="text-[11px] font-medium text-muted-foreground">
                    What the template asks for
                  </p>
                  {suggestions.map((s, i) => (
                    <FieldCard
                      key={s.field_id}
                      suggestion={s}
                      chosen={bindings[s.field_id] ?? ""}
                      columns={columns}
                      index={i}
                      policy={policy}
                      vetoLexicon={vetoLexicon}
                      overridden={!!overridden[s.field_id]}
                      active={hovered === s.field_id}
                      reduced={reduced}
                      onBind={bind}
                      onHover={setHovered}
                      registerRef={registerField}
                      onLayoutChange={bumpLayout}
                    />
                  ))}
                </div>

                <div ref={rightRailRef} className="space-y-1.5">
                  <p className="text-[11px] font-medium text-muted-foreground">
                    Columns in this spreadsheet
                  </p>
                  {columns.map((c, i) => {
                    const bound = boundColumns.has(c);
                    const band = columnBand[c];
                    return (
                      <motion.div
                        key={c}
                        ref={(el) => registerColumn(c, el)}
                        initial={reduced ? false : { opacity: 0, y: 6 }}
                        animate={{ opacity: 1, y: 0 }}
                        transition={{ duration: DUR.reveal, ease: EASE_OUT, delay: staggerDelay(i) }}
                        className={cn(
                          "relative z-10 flex items-center gap-2 rounded-md border bg-surface px-2 py-1.5",
                          bound ? "border-border" : "border-dashed border-border",
                        )}
                      >
                        {bound ? (
                          <span
                            aria-hidden
                            className="h-1.5 w-1.5 shrink-0 rounded-full"
                            style={{
                              background: (BAND_LINE[band] ?? BAND_LINE[UNSCORED]).colour,
                            }}
                          />
                        ) : (
                          <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-full border border-border" />
                        )}
                        <span className="min-w-0 flex-1 truncate font-mono text-[11px]" title={c}>{c}</span>
                        {!bound && (
                          <span className="shrink-0 text-[10px] text-muted-foreground/70">unused</span>
                        )}
                      </motion.div>
                    );
                  })}
                </div>
              </div>
            </>
          )}

          {suggestions.length > 0 && (
            <div className="pl-10 text-xs text-muted-foreground">
              {mappedCount} of {suggestions.length} mapped.
              {suggestions.some((s) => !s.column) && " Fields with no match need a column choosing by hand."}
            </div>
          )}
        </section>
      )}

      {/* 2 — generate */}
      {manifest && suggestions.length > 0 && (
        <section className="rounded-xl border border-border bg-background/40 p-4 space-y-3">
          <StepHeader step={2} active={step === 2} done={watch.job?.status === "completed"}
            title="Generate documents" hint="One document per row, checked before it is kept" />

          <div className="pl-10 space-y-2">
            <Button size="sm" onClick={generate}
                    disabled={!!busy || watch.running || !mappedCount || sheetBlocksGeneration}>
              {busy === "generate" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileText className="h-3.5 w-3.5" />}
              {watch.running ? "Generating…" : "Generate"}
            </Button>
            <p className="text-xs text-muted-foreground">
              This moves you to Documents, where the letters and the progress of the run both are.
            </p>
            {/* Repeated here rather than left further up the page. This is the
                last moment the reader can act on it, and the panel that says so
                is several screens above by the time they reach this button. It
                does not block: nothing about an unmapped branch value stops a
                batch, it only decides what comes out of one. */}
            {unmappedValueCount > 0 && (
              <div className="flex items-start gap-1.5 text-xs text-ai-uncertain">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>
                  {unmappedValueCount} spreadsheet value{unmappedValueCount === 1 ? "" : "s"} still match
                  no branch. Rows carrying {unmappedValueCount === 1 ? "it" : "them"} generate with the
                  conditional section missing.
                </span>
              </div>
            )}
            {sheetBlocksGeneration && (
              <div className="flex items-start gap-1.5 text-xs text-ai-uncertain">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>
                  Generating always reads “{sheets[0]}”, the first sheet in this workbook. Mapping
                  against “{sheet}” is safe to look over; switch back to “{sheets[0]}” to generate.
                </span>
              </div>
            )}
          </div>
        </section>
      )}

    </div>
  );
}
