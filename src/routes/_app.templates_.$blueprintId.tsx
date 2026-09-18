/**
 * The template editor: the document, and what the engine understood about it.
 *
 * Deliberately not a rich-text editor. The whole pipeline addresses text by
 * `(paragraph_index, span_index)` and tells a placeholder from a static run by
 * its colour, and a rich-text model preserves neither -- round-tripping a
 * template through HTML is exactly the defect the backend's `text_edit` module
 * was written to close. So this renders paragraphs and the runs inside them, and
 * an edit changes one run.
 *
 * Which turns out to be the right shape for the job anyway. The unit a person
 * wants to change here is "this placeholder" or "this instruction", not "these
 * three words spanning two runs", and the unit the engine fills is the run.
 */

import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { motion } from "framer-motion";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Download,
  EyeOff,
  History,
  Info,
  Sparkles,
  Loader2,
  MessageSquare,
  RotateCcw,
  Save,
  ScanLine,
  Send,
  Trash2,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import {
  CAP, DUR, EASE_IN_OUT, EASE_OUT, FadeIn, useReducedMotionFlag,
} from "@/components/motion";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { cn } from "@/lib/utils";
import type {
  Blueprint, BlueprintBlock, BlueprintBody, BlueprintParagraph, BlueprintSegment,
  LintFinding, LintReport, SegmentRole,
} from "@/lib/types";

// `templates_` rather than `templates`: the trailing underscore is TanStack
// Router's way of saying "do not nest me inside that route". Without it this
// registers as a child of the templates list page, which renders no `<Outlet />`
// -- so navigating here showed the list again and the editor never appeared,
// with no error anywhere to say why. The same convention the project route
// already uses for `$id_.edit`.
export const Route = createFileRoute("/_app/templates_/$blueprintId")({
  /** Where the user came from, when they came from a project.
   *
   *  Optional, because this screen is also reached from the flat Templates list
   *  and has to keep working with no origin at all. When it is present the Back
   *  link goes home instead of to a list the user was never on, and publishing
   *  returns them to the project rather than leaving them on the editor of a
   *  template they have finished with. */
  validateSearch: (search: Record<string, unknown>): { project?: string; template?: string } => {
    // Keys are omitted rather than set to undefined: a validator that always
    // returns both makes them *required* at every call site, and the three
    // existing links from the flat Templates list have no project to name.
    const out: { project?: string; template?: string } = {};
    if (typeof search.project === "string") out.project = search.project;
    if (typeof search.template === "string") out.template = search.template;
    return out;
  },
  component: TemplateEditorPage,
});

/** The same three roles the pre-scanner classifies runs into, and their colours. */
const ROLE_STYLE: Record<SegmentRole, { className: string; label: string }> = {
  static: { className: "text-foreground", label: "Static text" },
  placeholder: { className: "text-[var(--color-token-source)] font-medium", label: "Placeholder" },
  instruction: { className: "text-[var(--color-token-prompt)]", label: "Author instruction" },
  mergefield: { className: "text-[var(--color-token-repeat)] font-mono text-[0.85em]", label: "Merge field" },
  hyperlink: { className: "text-primary underline underline-offset-2", label: "Hyperlink" },
};

/**
 * The x-ray palette: one entry per classification the pre-scanner produces that
 * the stylesheet has a run colour for.
 *
 * Static runs are deliberately absent. They are what everything else is read
 * against, so under the x-ray they recede rather than light up -- a fourth
 * highlight would leave nothing to contrast with, which is the failure mode of
 * every "colour every token" view ever shipped.
 *
 * Written out as whole class strings rather than composed from the role name,
 * because Tailwind reads the source for literals: `bg-run-${role}/15` is a class
 * that never gets generated.
 */
const XRAY_STYLE: Partial<Record<SegmentRole, {
  label: string; overlay: string; glow: string; dot: string; chip: string; chipOn: string;
}>> = {
  placeholder: {
    label: "Placeholders",
    overlay: "bg-run-placeholder/14 ring-1 ring-run-placeholder/45",
    glow: "bg-run-placeholder/30",
    dot: "bg-run-placeholder",
    chip: "border-run-placeholder/35 text-run-placeholder hover:bg-run-placeholder/10",
    chipOn: "border-run-placeholder/70 bg-run-placeholder/15 text-run-placeholder",
  },
  instruction: {
    label: "Instructions",
    overlay: "bg-run-instruction/14 ring-1 ring-run-instruction/45",
    glow: "bg-run-instruction/30",
    dot: "bg-run-instruction",
    chip: "border-run-instruction/35 text-run-instruction hover:bg-run-instruction/10",
    chipOn: "border-run-instruction/70 bg-run-instruction/15 text-run-instruction",
  },
  mergefield: {
    label: "Merge fields",
    overlay: "bg-run-mergefield/14 ring-1 ring-run-mergefield/45",
    glow: "bg-run-mergefield/30",
    dot: "bg-run-mergefield",
    chip: "border-run-mergefield/35 text-run-mergefield hover:bg-run-mergefield/10",
    chipOn: "border-run-mergefield/70 bg-run-mergefield/15 text-run-mergefield",
  },
};

/** The classifications the x-ray paints, in the order the legend lists them. */
const XRAY_ROLES = ["placeholder", "instruction", "mergefield"] as const;

/** Milliseconds between one paragraph lighting up and the next.
 *
 *  Not `staggerDelay`, which returns zero past its twelfth item: a document has
 *  hundreds of paragraphs, so that cap made everything below paragraph 12 arrive
 *  at once and the cascade the reveal is for never happened on a real template.
 *  What bounds it here instead is `XRAY_ANIMATED_RUNS` -- a limit on how many
 *  runs animate at all -- with `XRAY_MAX_DELAY` as the backstop that holds the
 *  whole sequence inside `CAP.sequenceMs` however the runs fall. */
const XRAY_STEP_MS = 20;

/** How many painted runs get an animated reveal, in document order.
 *
 *  The overlay is the one thing on this screen that scales with the size of the
 *  customer's document: a 500-paragraph contract carries thousands of classified
 *  runs, and one animated layer each is thousands of composited nodes for a
 *  reveal nobody can see past the first screenful. Runs past this cap are still
 *  classified and still coloured -- they simply arrive already lit, which is what
 *  a run below the fold looks like by the time it is scrolled to anyway.
 *
 *  Sized to cover several screenfuls of a dense template, so what a reader can
 *  actually watch is the part that cascades. */
const XRAY_ANIMATED_RUNS = 120;

/** Latest a run may be scheduled, in seconds. `CAP.sequenceMs` is the budget for
 *  the whole sequence, so the last run has to *finish* inside it, not start. */
const XRAY_MAX_DELAY = CAP.sequenceMs / 1000 - DUR.revealSlow;

/** The overlay box itself: the tint and ring live on `XRAY_STYLE[role].overlay`,
 *  the geometry is the same whether or not this run is one of the animated ones. */
const XRAY_OVERLAY = "pointer-events-none absolute -inset-[2px] -z-10 rounded-[4px]";

/** What "dim" means under the x-ray. Opacity, not a grey: the run keeps the
 *  colour the document gave it and only recedes. */
const XRAY_DIM = "opacity-35";

/** Shortest instruction run that may be matched to a condition by containment.
 *  A three-character fragment is inside half the sentences in the document, and
 *  the wrong object id under the cursor is worse than no id at all. */
const MIN_JOINABLE_INSTRUCTION = 8;

/** Addresses one run for the DOM: the two coordinates the whole pipeline uses. */
const runKey = (path: number[], segmentIndex: number) => `${path.join(".")}:${segmentIndex}`;

/** Horizontal centre for something floating over `rect`, kept inside the
 *  viewport. `half` is how much room the overlay needs either side; on a window
 *  narrower than that it centres instead of pinning to an edge it cannot fit
 *  against. */
function clampCentre(rect: DOMRect, half: number) {
  const room = Math.min(half, Math.max(0, window.innerWidth / 2 - 8));
  return Math.min(Math.max(rect.left + rect.width / 2, room), window.innerWidth - room);
}

/** Whitespace-flattened and case-folded, which is how the compiler stores the
 *  text it read -- `" ".join(text.split())` on its side, this on ours. */
const squash = (text: string) => text.replace(/\s+/g, " ").trim().toLowerCase();

type ParagraphEntry = ReturnType<typeof walkParagraphs>[number];

type ObjectIndex = {
  /** Field slots, grouped by the paragraph they were addressed to. */
  fields: Map<number, { needle: string; code: string | null; object: Record<string, any> }[]>;
  /** Conditions, keyed on the instruction sentence they were compiled from --
   *  a condition carries no paragraph index, only the author's own words. */
  conditions: { needle: string; object: Record<string, any> }[];
};

/**
 * Index the version's §6 objects by where they touch the document.
 *
 * Built once per load so the hover tooltip does not walk every object on every
 * mouse move. The join is deliberately by paragraph index and text rather than
 * by `span_index`: a span index counts only the segments that become runs, so it
 * is not the segment index this editor addresses by, and quietly treating one as
 * the other would put a real object id under the wrong run.
 */
function indexObjects(objects: Record<string, any>[] | undefined | null): ObjectIndex {
  const fields: ObjectIndex["fields"] = new Map();
  const conditions: ObjectIndex["conditions"] = [];

  for (const object of objects ?? []) {
    if (object?.object_type === "FIELD") {
      for (const slot of (object.slots ?? []) as Record<string, any>[]) {
        const paragraph = slot?.paragraph_index;
        if (typeof paragraph !== "number") continue;
        const list = fields.get(paragraph) ?? [];
        list.push({
          needle: squash(String(slot.text ?? "")),
          code: slot.code ? squash(String(slot.code)) : null,
          object,
        });
        fields.set(paragraph, list);
      }
    } else if (object?.object_type === "CONDITION") {
      const needle = squash(String(object.compiled_from ?? ""));
      if (needle) conditions.push({ needle, object });
    }
  }
  return { fields, conditions };
}

/**
 * The object this run belongs to, or null -- and null is an answer, not a gap.
 *
 * The objects are the reading the compiler took of the document *as it was
 * loaded*. Edit a placeholder and the join stops matching, which is correct: the
 * tooltip then shows what the run is and says nothing about which object claims
 * it, rather than naming one that no longer describes these words.
 */
function objectForRun(
  paragraphIndex: number, segment: BlueprintSegment, index: ObjectIndex,
): Record<string, any> | null {
  const slots = index.fields.get(paragraphIndex) ?? [];

  if (segment.role === "mergefield") {
    const code = squash(segment.code ?? "");
    return code ? (slots.find((s) => s.code === code)?.object ?? null) : null;
  }

  if (segment.role === "placeholder") {
    const text = squash(segment.text);
    if (!text) return null;
    // Equal first, then contained: the compiler's slot text is the bracket token
    // it lifted out of the run -- "[Employee Name]" -- where the run itself may
    // be the whole of "Dear [Employee Name],".
    return (slots.find((s) => s.needle && s.needle === text)
      ?? slots.find((s) => s.needle && text.includes(s.needle)))?.object ?? null;
  }

  if (segment.role === "instruction") {
    const text = squash(segment.text);
    if (!text) return null;
    const exact = index.conditions.find((c) => c.needle === text);
    if (exact) return exact.object;
    // An inline switch joins several instruction runs into one sentence before
    // parsing it, so a run can be a fragment of what the condition was compiled
    // from -- but only a fragment long enough to mean something.
    if (text.length < MIN_JOINABLE_INSTRUCTION) return null;
    return index.conditions.find((c) => c.needle.includes(text))?.object ?? null;
  }

  return null;
}

const SEVERITY_STYLE: Record<string, string> = {
  blocking: "border-destructive/40 bg-destructive/10 text-destructive",
  warning: "border-amber-500/40 bg-amber-500/10 text-amber-500",
  advisory: "border-border bg-muted/40 text-muted-foreground",
};

/** Paragraphs in the order the backend indexes them: document order, into tables. */
function walkParagraphs(body: BlueprintBody | undefined) {
  const out: { index: number; block: BlueprintParagraph; inTable: boolean; path: number[] }[] = [];
  const walk = (blocks: BlueprintBlock[], inTable: boolean, path: number[]) => {
    blocks.forEach((block, i) => {
      if (block.kind === "paragraph") {
        out.push({ index: out.length, block, inTable, path: [...path, i] });
      } else {
        block.rows.forEach((row, r) =>
          row.forEach((cell, c) => walk(cell, true, [...path, i, r, c])));
      }
    });
  };
  walk(body?.blocks ?? [], false, []);
  return out;
}

/** Replace one segment, without mutating anything the caller still holds. */
function withSegment(
  body: BlueprintBody, path: number[], segmentIndex: number,
  change: (segment: BlueprintSegment) => BlueprintSegment,
): BlueprintBody {
  const replaceIn = (blocks: BlueprintBlock[], remaining: number[]): BlueprintBlock[] => {
    const [head, ...rest] = remaining;
    return blocks.map((block, i) => {
      if (i !== head) return block;
      if (block.kind === "paragraph") {
        return {
          ...block,
          segments: block.segments.map((s, si) => (si === segmentIndex ? change(s) : s)),
        };
      }
      const [rowIndex, cellIndex, ...deeper] = rest;
      return {
        ...block,
        rows: block.rows.map((row, r) => r !== rowIndex ? row : row.map((cell, c) =>
          c !== cellIndex ? cell : replaceIn(cell, deeper))),
      };
    });
  };
  return { ...body, blocks: replaceIn(body.blocks, path) };
}

function TemplateEditorPage() {
  const { blueprintId } = Route.useParams();
  const { project: fromProject } = Route.useSearch();
  const navigate = useNavigate();

  const [blueprint, setBlueprint] = useState<Blueprint | null>(null);
  const [body, setBody] = useState<BlueprintBody | null>(null);
  const [dirty, setDirty] = useState(false);
  const [selected, setSelected] = useState<{ path: number[]; segmentIndex: number } | null>(null);
  const [lint, setLint] = useState<LintReport | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [versions, setVersions] = useState<any[]>([]);
  const [showVersions, setShowVersions] = useState(false);
  /** Asked before publishing when the lint panel has blocking findings, because
   *  re-reading skips the server's own gate on them. */
  const [confirmPublish, setConfirmPublish] = useState(false);
  /** The model could not read the edited template. Nothing was published, and
   *  the fallback the server's message names is offered from here. */
  const [readFailure, setReadFailure] = useState<string | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);

  /** The x-ray: the classification overlay, and which classification (if any)
   *  the legend is filtering to. Both are a way of looking at the body already
   *  loaded -- nothing here is fetched, nothing is saved, and neither survives a
   *  reload, which is why they are local state and not the store. */
  const [xray, setXray] = useState(false);
  const [xrayFilter, setXrayFilter] = useState<SegmentRole | null>(null);
  /** True for the one sweep that runs when the x-ray comes on. */
  const [sweeping, setSweeping] = useState(false);
  /** The run whose selection scale-in has not been released yet.
   *
   *  Two fields rather than one flag because a CSS transition needs a frame
   *  sitting at the start value before it has anything to travel from, and the
   *  timestamp because clicking the same run twice has to replace the object --
   *  otherwise the second click is a no-op and the second press does not
   *  register. */
  const [pop, setPop] = useState<{ key: string; at: number; released: boolean } | null>(null);
  /** Open only when a run was clicked in the document. A selection made from the
   *  findings list gets the inspector and no floating toolbar: that run may be
   *  scrolled far out of view, and a toolbar anchored to something nobody can
   *  see is a toolbar in the wrong place. */
  const [toolbarOpen, setToolbarOpen] = useState(false);
  const docRef = useRef<HTMLDivElement | null>(null);
  const reduced = useReducedMotionFlag();

  const load = useCallback(async () => {
    const bp = await api.getBlueprint(blueprintId);
    setBlueprint(bp);
    setBody(bp.version?.body ?? null);
    setDirty(false);
    setSelected(null);
    api.lintBlueprint(blueprintId).then(setLint).catch(() => setLint(null));
    api.listBlueprintVersions(blueprintId).then((r) => setVersions(r.items)).catch(() => {});
  }, [blueprintId]);

  useEffect(() => {
    load().catch((e: any) =>
      toast.error("Could not open this template", { description: e?.message ?? String(e) }));
  }, [load]);

  const paragraphs = useMemo(() => walkParagraphs(body ?? undefined), [body]);

  const selectedSegment: BlueprintSegment | null = useMemo(() => {
    if (!selected || !body) return null;
    const entry = paragraphs.find((p) => p.path.join(".") === selected.path.join("."));
    return entry?.block.segments[selected.segmentIndex] ?? null;
  }, [selected, paragraphs, body]);

  /** The legend's counts, taken from the same `body` the document is rendered
   *  from -- so a chip and the overlays under it cannot disagree, whatever the
   *  objects or the last lint happen to say.
   *
   *  Counted over the whole body, and they stay that way: `XRAY_ANIMATED_RUNS`
   *  caps how many runs *animate*, not how many are painted. Every run the
   *  legend counts is coloured on the page below it, so the chip and the document
   *  still describe the same set. */
  const runCounts = useMemo(() => {
    const counts: Record<SegmentRole, number> = {
      static: 0, placeholder: 0, instruction: 0, mergefield: 0, hyperlink: 0,
    };
    for (const entry of paragraphs) {
      for (const segment of entry.block.segments) counts[segment.role] += 1;
    }
    return counts;
  }, [paragraphs]);

  const objectIndex = useMemo(
    () => indexObjects(blueprint?.version?.objects), [blueprint?.version?.objects]);

  /**
   * Which painted runs animate their reveal, and when each one arrives.
   *
   * A `Map` from run key to delay in seconds; absent means "paint it, do not
   * animate it". Built in document order and stopped at `XRAY_ANIMATED_RUNS`, so
   * the animated runs are always a prefix of the document -- the cascade cannot
   * skip a paragraph and resume below it.
   *
   * The delay is per *paragraph*, not per run: every painted run in a paragraph
   * lights up together and the next paragraph follows `XRAY_STEP_MS` later, which
   * is what makes the sweep read top-to-bottom rather than word-by-word along a
   * line. Ranked over the paragraphs that actually carry a painted run, so a
   * template whose first placeholder is on paragraph 90 still cascades from it
   * rather than waiting out 90 empty steps.
   *
   * Empty under reduced motion, which is how the whole overlay stops moving
   * without a second branch at the render site.
   */
  const xrayReveal = useMemo(() => {
    const delays = new Map<string, number>();
    if (!xray || reduced) return delays;
    let rank = -1;
    let animated = 0;
    for (const entry of paragraphs) {
      let paragraphStarted = false;
      for (let si = 0; si < entry.block.segments.length; si += 1) {
        const segment = entry.block.segments[si];
        if (XRAY_STYLE[segment.role] == null) continue;
        if (xrayFilter != null && xrayFilter !== segment.role) continue;
        if (animated >= XRAY_ANIMATED_RUNS) return delays;
        if (!paragraphStarted) {
          paragraphStarted = true;
          rank += 1;
        }
        delays.set(runKey(entry.path, si),
                   Math.min((rank * XRAY_STEP_MS) / 1000, XRAY_MAX_DELAY));
        animated += 1;
      }
    }
    return delays;
  }, [xray, reduced, xrayFilter, paragraphs]);

  /** Release the selection scale-in on the frame after the click. */
  useEffect(() => {
    if (!pop || pop.released) return;
    const frame = requestAnimationFrame(() =>
      setPop((p) => (p && !p.released ? { ...p, released: true } : p)));
    return () => cancelAnimationFrame(frame);
  }, [pop]);

  /** Turning the x-ray off puts the document back exactly as it was: no filter
   *  left applied to a view that is no longer showing why. */
  useEffect(() => {
    if (xray) return;
    setXrayFilter(null);
    setSweeping(false);
  }, [xray]);

  const findingsByParagraph = useMemo(() => {
    const map = new Map<number, LintFinding[]>();
    for (const f of lint?.findings ?? []) {
      if (f.paragraph_index == null) continue;
      map.set(f.paragraph_index, [...(map.get(f.paragraph_index) ?? []), f]);
    }
    return map;
  }, [lint]);

  /** One sweep per activation. The sweep is a reading, not a process: the
   *  classification was already in the body before the toggle was touched. */
  const toggleXray = () => {
    const next = !xray;
    setXray(next);
    setSweeping(next && !reduced);
  };

  const update = (change: (segment: BlueprintSegment) => BlueprintSegment) => {
    if (!selected || !body) return;
    setBody(withSegment(body, selected.path, selected.segmentIndex, change));
    setDirty(true);
  };

  const save = async () => {
    if (!body || !blueprint) return;
    setBusy("save");
    try {
      await api.saveBlueprint(blueprintId, {
        body,
        change_summary: "Edited in the template editor.",
        expected_version_no: blueprint.version_no ?? undefined,
      });
      toast.success("Saved");
      await load();
    } catch (e: any) {
      toast.error(
        e?.code === "BLUEPRINT_VERSION_CONFLICT" ? "Somebody else saved first" : "Could not save",
        { description: e?.message ?? String(e), duration: 8000 });
    } finally { setBusy(null); }
  };

  const download = async () => {
    setBusy("download");
    try {
      const url = await api.blueprintDocxUrl(blueprintId);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${blueprint?.name ?? "template"}.docx`.replace(/\.docx\.docx$/, ".docx");
      a.click();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      toast.error("Could not download", { description: e?.message ?? String(e) });
    } finally { setBusy(null); }
  };

  /**
   * Write the edited template and have the compiler read it again.
   *
   * `recompile` is the default rather than a second button, because it is what
   * makes an edit take effect. The blueprint's own objects are a reading of the
   * document as it was *before* the edit; publishing from them writes a manifest
   * that describes the old wording, which is how you repair a placeholder and
   * watch nothing change. Re-reading means the manifest that ships is a reading
   * of the bytes that shipped.
   *
   * Two things follow from that and are handled below rather than hidden:
   *
   * `recompile` skips the publishability lint gate -- deliberately, on the
   * server: the objects are not what ships on this path, so refusing on them
   * would refuse a template that is now correct for a reading that is merely out
   * of date. Which means this button is not lint-gated, so a blocking finding is
   * confirmed here instead.
   *
   * And re-reading needs a language model, so it can fail with `TEMPLATE_NOT_READ`
   * when one is unreachable. The server's own message tells the reader to publish
   * without re-reading, so that route has to exist: `fallback` is it, and it *is*
   * lint-gated.
   */
  const publish = async (recompile = true) => {
    setBusy(recompile ? "publish" : "fallback");
    try {
      const result = await api.publishBlueprint(blueprintId, [], recompile);
      setLint(result.lint);
      setConfirmPublish(false);
      setReadFailure(null);

      // Compiling approves its own reading where it may, and publishing takes
      // the same path -- so in the ordinary case the new version is live the
      // moment this returns. Where it could not (a role without the capability,
      // or a legally binding template) the server says why, and repeating that
      // here is the difference between "published" and "published, and still not
      // the version your project generates from".
      const pending = (result as any)?.approval_blocked_reason as string | null | undefined;
      toast.success(
        recompile ? "Template regenerated — updated version published" : "Published",
        {
          description: pending
            ? `${pending} Until then, generation still uses the previous version.`
            : "The template and the rules that fill it are both live.",
          duration: pending ? 10000 : 5000,
        },
      );

      // Back to where they came from. Finishing an edit is finishing with this
      // screen, and leaving the user on it after a successful publish is leaving
      // them to work out for themselves that it is over.
      if (fromProject) {
        navigate({ to: "/projects/$id", params: { id: fromProject } });
      } else {
        navigate({ to: "/templates" });
      }
      return;
    } catch (e: any) {
      const report = e?.details?.lint as LintReport | undefined;
      if (report) setLint(report);
      if (e?.code === "TEMPLATE_NOT_READ" || e?.code === "LLM_NOT_CONFIGURED") {
        // Nothing was published -- the server compiles before it writes, so the
        // template is exactly as it was. Offer the route its own message names.
        setReadFailure(e?.message ?? String(e));
        return;
      }
      toast.error("Not ready to publish", {
        description: e?.message ?? String(e), duration: 10000,
      });
    } finally { setBusy(null); }
  };

  if (!blueprint || !body) {
    return (
      <div className="flex h-[60vh] items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Opening the template…
      </div>
    );
  }

  const blocking = lint?.findings.filter((f) => f.severity === "blocking") ?? [];

  return (
    <div className="mx-auto max-w-[1500px] space-y-4 p-6 lg:p-8">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          {fromProject ? (
            <Link
              to="/projects/$id"
              params={{ id: fromProject }}
              className="mb-1 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            >
              <ArrowLeft className="h-3 w-3" /> Back to project
            </Link>
          ) : (
            <Link to="/templates" className="mb-1 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
              <ArrowLeft className="h-3 w-3" /> Templates
            </Link>
          )}
          <h1 className="truncate text-2xl font-semibold tracking-tight">{blueprint.name}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Badge variant="outline">v{blueprint.version_no}</Badge>
            <Badge variant={blueprint.status === "published" ? "default" : "secondary"}>
              {blueprint.status}
            </Badge>
            <span>{paragraphs.length} paragraphs</span>
            {blueprint.kind === "legacy" && (
              <span className="inline-flex items-center gap-1">
                <Info className="h-3 w-3" />
                read from an uploaded file, which is kept so this can be put back
              </span>
            )}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {/* Paints the classification the pre-scanner already made onto the
              runs it made it about. Nothing is fetched and nothing is asked --
              the body in this editor is where every one of those colours and
              counts comes from. */}
          <Button
            variant={xray ? "secondary" : "outline"}
            size="sm"
            aria-pressed={xray}
            onClick={toggleXray}
            className={cn("gap-1.5", xray && "border border-ai-active/50 text-ai-active")}
            title="Colour every run by what the engine classified it as"
          >
            <ScanLine className="h-3.5 w-3.5" /> X-ray
          </Button>
          <Button variant="outline" size="sm" onClick={() => setShowVersions((s) => !s)} className="gap-1.5">
            <History className="h-3.5 w-3.5" /> Versions
          </Button>
          <Button variant="outline" size="sm" onClick={download} disabled={busy != null} className="gap-1.5">
            {busy === "download" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
            Download .docx
          </Button>
          <Button size="sm" onClick={save} disabled={!dirty || busy != null} className="gap-1.5">
            {busy === "save" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
            {dirty ? "Save" : "Saved"}
          </Button>
          {/* One action, where there were two.
              The old pair was "Publish" and, for legacy templates only,
              "Publish & re-read" -- and the first of them wrote a manifest
              describing the wording as it was before the edit. Re-reading is
              what makes an edit take effect, so it is not an option. */}
          <Button size="sm" className="gap-1.5"
                  onClick={() => (blocking.length ? setConfirmPublish(true) : publish(true))}
                  disabled={busy != null || dirty}
                  title="Write the edited template, have the compiler read it again, and publish the result">
            {busy === "publish" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
            Publish &amp; regenerate updated template
          </Button>
        </div>
      </div>

      {/* Re-reading skips the server's publishability gate on purpose -- the
          objects are not what ships -- so a blocking finding has to be confirmed
          here or it is not raised at all. */}
      <AlertDialog open={confirmPublish} onOpenChange={(o) => { if (busy == null) setConfirmPublish(o); }}>
        <AlertDialogContent className="border-border bg-surface">
          <AlertDialogHeader>
            <AlertDialogTitle>
              Publish with {blocking.length} unresolved {blocking.length === 1 ? "finding" : "findings"}?
            </AlertDialogTitle>
            <AlertDialogDescription className="whitespace-pre-line">
              {"The compiler reads the document again on the way out, so these may no longer apply — "
               + "they describe the reading this template is carrying now, not the one that will ship. "
               + "But nothing checks them again, so if they are real they will reach the letters.\n\n"
               + blocking.slice(0, 3).map((f) => f.detail).join("\n")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy != null}>Go back</AlertDialogCancel>
            <AlertDialogAction disabled={busy != null}
                               onClick={(e) => { e.preventDefault(); void publish(true); }}>
              {busy != null ? "Publishing…" : "Publish anyway"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* The compile runs before anything is written, so this means the template
          is exactly as it was. The server's own message says to publish without
          re-reading; that has to be reachable, and it is lint-gated. */}
      <AlertDialog open={readFailure != null} onOpenChange={(o) => { if (!o && busy == null) setReadFailure(null); }}>
        <AlertDialogContent className="border-border bg-surface">
          <AlertDialogHeader>
            <AlertDialogTitle>The edited template could not be read</AlertDialogTitle>
            <AlertDialogDescription className="whitespace-pre-line">
              {`${readFailure ?? ""}\n\nNothing was published — your template is exactly as it was. `
               + "You can publish without re-reading, which ships the reading this editor is carrying "
               + "rather than a fresh one. Every check runs on that path, so it refuses if the reading "
               + "no longer matches the document."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy != null}>Leave it</AlertDialogCancel>
            <AlertDialogAction disabled={busy != null}
                               onClick={(e) => { e.preventDefault(); void publish(false); }}>
              {busy === "fallback" ? "Publishing…" : "Publish without re-reading"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {showVersions && (
        <div className="rounded-xl surface-raised p-4">
          <p className="mb-2 text-xs uppercase tracking-wider text-muted-foreground">History</p>
          <div className="divide-y divide-border">
            {versions.map((v) => (
              <div key={v.id} className="flex items-center justify-between gap-3 py-2 text-sm">
                <div className="min-w-0">
                  <span className="font-medium">v{v.version_no}</span>
                  <span className="ml-2 text-muted-foreground">{v.change_summary}</span>
                </div>
                {v.version_no !== blueprint.version_no && (
                  <Button variant="ghost" size="sm" className="gap-1.5"
                          onClick={async () => {
                            await api.revertBlueprint(blueprintId, v.version_no);
                            toast.success(`Forked a new version from v${v.version_no}`);
                            await load();
                          }}>
                    <RotateCcw className="h-3.5 w-3.5" /> Put back to this
                  </Button>
                )}
              </div>
            ))}
          </div>
          <p className="mt-2 text-[11px] text-muted-foreground">
            Putting back forks a new version rather than erasing the ones after it — the history is
            a record of the work, not of the current opinion.
          </p>
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_360px]">
        {/* The document */}
        <div className="min-w-0 space-y-3">
          {xray && (
            <XrayLegend counts={runCounts} filter={xrayFilter} onFilter={setXrayFilter} />
          )}

          {/* Wrapped so the sweep can sit over the panel's visible box rather
              than over its scrolling content, where it would be somewhere up
              near paragraph 1 the moment the reader scrolled. */}
          <div className="relative">
            <div
              ref={docRef}
              className={cn("overflow-auto rounded-xl surface-raised p-6",
                            xray ? "max-h-[calc(100vh-360px)]" : "max-h-[calc(100vh-260px)]")}
            >
              {paragraphs.map(({ index, block, inTable, path }) => {
                const found = findingsByParagraph.get(index) ?? [];
                return (
                  <div key={index}
                       className={cn("group relative -mx-2 rounded px-2 py-0.5",
                                     inTable && "border-l-2 border-border/70 pl-3",
                                     found.some((f) => f.severity === "blocking") && "bg-destructive/5")}>
                    <span className="pointer-events-none absolute -left-9 top-1 hidden text-[10px] tabular-nums text-muted-foreground group-hover:block">
                      {index}
                    </span>
                    <p className={cn("min-h-[1.4em] leading-relaxed",
                                     block.style?.startsWith("Heading") && "mt-4 text-lg font-semibold")}>
                      {block.segments.map((segment, si) => {
                        const key = runKey(path, si);
                        const isSelected = selected?.path.join(".") === path.join(".")
                          && selected?.segmentIndex === si;
                        const style = ROLE_STYLE[segment.role];
                        const hidden = segment.emit === false;
                        const lit = XRAY_STYLE[segment.role];
                        // Under the x-ray a run is either painted or dimmed, and
                        // a legend chip narrows "painted" to one classification.
                        // The dim is the point: it is what makes three colours in
                        // a page of prose legible as a pattern.
                        const painted = xray && lit != null
                          && (xrayFilter == null || xrayFilter === segment.role);
                        const dimmed = xray && !painted;
                        const popping = pop?.key === key && !pop.released;
                        // Undefined for a run past the animation cap, and for
                        // every run under reduced motion.
                        const revealAt = painted ? xrayReveal.get(key) : undefined;
                        return (
                          <button
                            key={si}
                            data-run={key}
                            data-role={segment.role}
                            data-paragraph={index}
                            data-segment={si}
                            onClick={() => {
                              setSelected({ path, segmentIndex: si });
                              setToolbarOpen(true);
                              setPop({ key, at: Date.now(), released: false });
                            }}
                            className={cn(
                              // Hover lifts one pixel into a soft ring; selection
                              // travels back from 0.96. Both are CSS transforms
                              // rather than framer-motion, so they cost nothing
                              // per run in a document with a thousand of them,
                              // and both are already covered by the global
                              // reduced-motion block in styles.css. The x-ray
                              // overlay below is the only framer-motion node in
                              // this loop, and `xrayReveal` bounds how many of
                              // those a document can produce.
                              "relative isolate rounded px-0.5 text-left align-baseline",
                              "transition-[translate,scale,opacity,color,background-color,box-shadow]",
                              "duration-[250ms] ease-out",
                              "hover:-translate-y-px hover:bg-accent hover:ring-1 hover:ring-border-strong/70",
                              style.className,
                              hidden && "line-through opacity-40",
                              dimmed && XRAY_DIM,
                              isSelected && "bg-primary/20 ring-1 ring-primary",
                              // The frame that sits at the start value. It has to
                              // land instantly, or the run spends 250ms shrinking
                              // before it is allowed to come back.
                              popping && "scale-[0.96] duration-0",
                            )}
                            title={xray ? undefined
                              : hidden ? `${style.label} — removed from the published template`
                              : style.label}
                          >
                            {painted && lit && (revealAt == null ? (
                              // Past the cap: the same tint and the same ring,
                              // already lit. The classification is the feature
                              // and it is intact for every run in the document;
                              // what is capped is the reveal, which is a way of
                              // showing the reader where to look and is spent by
                              // the time it reaches here.
                              <span aria-hidden className={cn(XRAY_OVERLAY, lit.overlay)} />
                            ) : (
                              <motion.span
                                aria-hidden
                                className={cn(XRAY_OVERLAY, lit.overlay)}
                                initial={{ opacity: 0, scale: 1.08 }}
                                animate={{ opacity: 1, scale: 1 }}
                                transition={{ duration: DUR.revealSlow, delay: revealAt, ease: EASE_OUT }}
                              >
                                {/* The settle -- and the reason it is a halo now
                                    and not `blur-[5px]`. A blur is a filter pass
                                    per node per frame, and this one stayed on
                                    every painted run for as long as the x-ray was
                                    on: the cost was the steady state, not the
                                    reveal. This is opacity and transform only, it
                                    is bounded by the same cap as its parent, and
                                    once it has played there is nothing left to
                                    composite.

                                    It grows *into* the run's outline rather than
                                    out of it. An absolutely positioned child
                                    counts toward the scroll panel's overflow, so
                                    a halo scaling past its parent on a run near
                                    the right margin would flick a horizontal
                                    scrollbar in and out mid-reveal -- which is
                                    the jank this whole change is about. */}
                                <motion.span
                                  aria-hidden
                                  className={cn("absolute -inset-[3px] -z-10 rounded-[6px]", lit.glow)}
                                  initial={{ opacity: 0.55, scale: 0.96 }}
                                  animate={{ opacity: 0, scale: 1 }}
                                  transition={{ duration: DUR.revealSlow, delay: revealAt, ease: EASE_OUT }}
                                />
                              </motion.span>
                            ))}
                            {segment.role === "mergefield" ? `«${segment.code}»` : segment.text || " "}
                          </button>
                        );
                      })}
                    </p>
                  </div>
                );
              })}
            </div>

            {/* One sweep on activation, and then it stops. The `scan-sweep`
                utility loops forever, which is right for a banner meaning "work
                is still happening" and wrong here: nothing is running. The
                classification was already in the body before the toggle was
                touched, so a repeating sweep would animate a process that does
                not exist. */}
            {xray && sweeping && !reduced && (
              <div aria-hidden
                   className="pointer-events-none absolute inset-0 overflow-hidden rounded-xl">
                {/* Travels on `y` alone: the band is a third of the panel tall,
                    so -100% starts it just above the top edge and 300% carries
                    it just past the bottom, with no height animated anywhere. */}
                <motion.div
                  className="absolute inset-x-0 h-1/3 bg-gradient-to-b from-transparent via-ai-active/12 to-transparent"
                  initial={{ y: "-100%", opacity: 0 }}
                  animate={{ y: "300%", opacity: [0, 1, 1, 0] }}
                  transition={{
                    duration: 0.9,
                    ease: EASE_IN_OUT,
                    // Scoped to opacity: `times` has to match the keyframe count
                    // of the value it belongs to, and `y` has two.
                    opacity: { duration: 0.9, times: [0, 0.15, 0.85, 1], ease: "linear" },
                  }}
                  onAnimationComplete={() => setSweeping(false)}
                />
              </div>
            )}

            <XrayTooltip container={docRef} active={xray} paragraphs={paragraphs}
                         objectIndex={objectIndex} />

            {toolbarOpen && selected && selectedSegment && (
              <RunToolbar
                container={docRef}
                anchorKey={runKey(selected.path, selected.segmentIndex)}
                segment={selectedSegment}
                onRole={(role) => update((s) => ({ ...s, role }))}
                onEmit={(leaveOut) => update((s) => ({ ...s, emit: leaveOut ? false : undefined }))}
                onDismiss={() => setToolbarOpen(false)}
              />
            )}
          </div>
        </div>

        {/* Inspector + findings */}
        <div className="space-y-4">
          <div className="rounded-xl surface-raised p-4">
            <p className="mb-3 text-xs uppercase tracking-wider text-muted-foreground">Selected run</p>
            {!selectedSegment ? (
              <p className="text-sm text-muted-foreground">
                Click any run in the document to see what the engine thinks it is, and change it.
              </p>
            ) : (
              <div className="space-y-3">
                <div className="flex flex-wrap gap-1.5">
                  {(["static", "placeholder", "instruction"] as SegmentRole[]).map((role) => (
                    <button
                      key={role}
                      disabled={selectedSegment.role === "mergefield" || selectedSegment.role === "hyperlink"}
                      onClick={() => update((s) => ({ ...s, role }))}
                      className={cn(
                        "rounded-full border px-2.5 py-1 text-xs font-medium transition-colors disabled:opacity-40",
                        selectedSegment.role === role
                          ? "border-primary bg-primary text-primary-foreground"
                          : "border-border hover:bg-accent",
                      )}
                    >
                      {ROLE_STYLE[role].label}
                    </button>
                  ))}
                </div>

                {selectedSegment.role === "mergefield" ? (
                  <Input value={selectedSegment.code ?? ""} readOnly className="font-mono text-xs" />
                ) : (
                  <Textarea
                    value={selectedSegment.text}
                    onChange={(e) => update((s) => ({ ...s, text: e.target.value }))}
                    rows={3}
                    className="text-sm"
                  />
                )}

                {selectedSegment.role === "instruction" && (
                  <label className="flex items-start gap-2 text-xs text-muted-foreground">
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={selectedSegment.emit === false}
                      onChange={(e) => update((s) => ({ ...s, emit: e.target.checked ? false : undefined }))}
                    />
                    <span>
                      Leave this out of the published template. The compile marks the instructions
                      it recognised; you decide whether each one was written for you or for the
                      reader.
                    </span>
                  </label>
                )}
              </div>
            )}
          </div>

          <CopilotPanel blueprintId={blueprintId} versionNo={blueprint.version_no ?? 1}
                        onApplied={load} />

          <div className="rounded-xl surface-raised p-4">
            <div className="mb-3 flex items-center justify-between">
              <p className="text-xs uppercase tracking-wider text-muted-foreground">Will this work?</p>
              {lint && (lint.can_publish
                ? <span
                    className="inline-flex items-center gap-1 text-xs text-emerald-500"
                    title="These checks read the blueprint. Publishing runs a stricter set against the document it writes, so it can still refuse."
                  >
                    {/* Not "ready": this pass writes no document, and the check
                        that asks an emitted file what is still wrong only runs
                        at publish. Saying ready here and refusing there is the
                        contradiction that made the refusal look like a bug. */}
                    <CheckCircle2 className="h-3.5 w-3.5" /> nothing flagged here
                  </span>
                : <span className="inline-flex items-center gap-1 text-xs text-destructive">
                    <AlertTriangle className="h-3.5 w-3.5" /> {lint.blocking} blocking
                  </span>)}
            </div>
            {/* Said where the blocking findings are actually read, because a
                refusal with no alternative reads as "this template cannot be
                published" -- which is false when the document is fine and only
                the reading is out of date. */}
            {blueprint.kind === "legacy" && (
              <div className="mb-3 rounded-lg border border-border bg-background/40 p-2.5 text-xs">
                <p className="text-muted-foreground">
                  These checks read the draft, not the finished file. Publishing has the engine
                  read the document it writes and build its rules from <em>that</em>, so a finding
                  here describes the reading this editor is carrying rather than the one that will
                  ship — and one you have already fixed in the document may still be
                  listed. They are worth reading before you publish, not worth being stopped by.
                </p>
              </div>
            )}
            {!lint?.findings.length ? (
              <p className="text-sm text-muted-foreground">
                Nothing to flag. Every placeholder is claimed, every condition reads something, and
                every field has somewhere in the document to go.
              </p>
            ) : (
              <div className="max-h-[40vh] space-y-2 overflow-auto">
                {lint.findings.map((f, i) => (
                  <button
                    key={i}
                    onClick={() => {
                      const target = paragraphs.find((p) => p.index === f.paragraph_index);
                      if (!target) return;
                      setSelected({ path: target.path, segmentIndex: 0 });
                      // The inspector answers this one. The floating toolbar
                      // stays shut, because the run a finding names can be a
                      // hundred paragraphs up the panel and a toolbar anchored
                      // to something nobody can see is a toolbar in the wrong
                      // place.
                      setToolbarOpen(false);
                    }}
                    className={cn("w-full rounded-lg border p-2.5 text-left text-xs",
                                  SEVERITY_STYLE[f.severity] ?? SEVERITY_STYLE.advisory)}
                  >
                    <div className="mb-0.5 flex items-center gap-1.5 font-mono text-[10px] uppercase opacity-70">
                      {f.severity} · {f.code}
                      {f.paragraph_index != null && <span>· paragraph {f.paragraph_index}</span>}
                    </div>
                    <div className="leading-snug">{f.detail}</div>
                  </button>
                ))}
              </div>
            )}
          </div>

          {blueprint.status === "published" && (
            <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-4 text-xs">
              <p className="mb-1 flex items-center gap-1.5 font-medium text-emerald-500">
                <CheckCircle2 className="h-3.5 w-3.5" /> Published
              </p>
              {/* This used to say the manifest was still a draft and point at a
                  screen to go and approve it on. Publishing now approves what it
                  writes, so the warning is no longer true -- and where it cannot
                  approve (a role without the capability, a legally binding
                  template) the publish toast says so at the moment it happens,
                  which is where that belongs. */}
              <p className="text-muted-foreground">
                The template and the rules that fill it are both live. Documents generated from
                this project use this version.
              </p>
            </div>
          )}

          <DeleteTemplatePanel
            blueprintId={blueprintId}
            name={blueprint.name}
            open={deleteOpen}
            onOpenChange={setDeleteOpen}
            onGone={() => navigate(
              fromProject
                ? { to: "/projects/$id", params: { id: fromProject } }
                : { to: "/templates" },
            )}
          />
        </div>
      </div>
    </div>
  );
}


/**
 * The x-ray legend: what the colours mean, how many of each there are, and a
 * filter per classification.
 *
 * The counts come from the same `body` the document above is rendered from,
 * passed down already totalled -- so a chip and the runs under it are counting
 * the same thing and cannot disagree. Nothing here is fetched, and there is no
 * figure on this surface the editor did not derive from the document it is
 * showing.
 */
function XrayLegend({ counts, filter, onFilter }: {
  counts: Record<SegmentRole, number>;
  filter: SegmentRole | null;
  onFilter: (role: SegmentRole | null) => void;
}) {
  return (
    <FadeIn className="rounded-xl surface-raised p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1.5 pr-1 text-xs uppercase tracking-wider text-muted-foreground">
          <ScanLine className="h-3.5 w-3.5" /> X-ray
        </span>

        {XRAY_ROLES.map((role) => {
          const style = XRAY_STYLE[role]!;
          const count = counts[role];
          const on = filter === role;
          return (
            <button
              key={role}
              type="button"
              // A classification with nothing in it is not a filter worth
              // offering: it would dim the whole document to say "none".
              disabled={count === 0}
              aria-pressed={on}
              onClick={() => onFilter(on ? null : role)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
                "transition-[scale,color,background-color,border-color] duration-150 ease-out",
                "active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40",
                on ? style.chipOn : style.chip,
              )}
              title={count === 0 ? undefined : on ? "Show every classification again"
                                                  : `Dim everything that is not ${style.label.toLowerCase()}`}
            >
              <span className={cn("h-1.5 w-1.5 rounded-full", style.dot)} />
              {style.label}
              <span className="tabular-nums opacity-70">{count}</span>
            </button>
          );
        })}

        {/* Static runs and links are not chips: they are what the highlighted
            runs are read against, and a filter that dimmed everything except the
            prose would be a filter for nothing. */}
        <span className="inline-flex items-center gap-1.5 pl-1 text-xs text-muted-foreground">
          <span className="h-1.5 w-1.5 rounded-full bg-run-static" />
          Static text <span className="tabular-nums">{counts.static}</span>
        </span>
        {counts.hyperlink > 0 && (
          <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground" />
            Links <span className="tabular-nums">{counts.hyperlink}</span>
          </span>
        )}

        {filter && (
          <button type="button" onClick={() => onFilter(null)}
                  className="ml-auto inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
            <X className="h-3 w-3" /> Clear filter
          </button>
        )}
      </div>

      <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
        Blue runs are placeholders, red runs are author instructions — that is the classification,
        read from the colours the document itself carries. Merge fields are Word&rsquo;s own.
        The x-ray covers the body shown below; headers and footers are not part of this view.
      </p>
    </FadeIn>
  );
}


/**
 * What one highlighted run is, floating beside it.
 *
 * Listens on the panel rather than on each run, for two reasons. A thousand-run
 * document would otherwise carry a thousand hover handlers, and -- worse --
 * hover state held on the page would re-render every paragraph on every mouse
 * move across the document. Delegation keeps the whole cost inside this
 * component, which is the only thing that has to change when the pointer moves.
 *
 * Everything it shows is either on the segment or on the object the compiler
 * built from it. There is no human-readable field name and no plain-English
 * rendering of a condition in this data, so neither is invented here: a
 * condition shows the expression it actually holds.
 */
function XrayTooltip({ container, active, paragraphs, objectIndex }: {
  container: RefObject<HTMLDivElement | null>;
  active: boolean;
  paragraphs: ParagraphEntry[];
  objectIndex: ObjectIndex;
}) {
  const [at, setAt] = useState<
    { rect: DOMRect; paragraphIndex: number; segmentIndex: number } | null>(null);

  useEffect(() => {
    const root = container.current;
    if (!root || !active) {
      setAt(null);
      return;
    }
    const onPointer = (event: Event) => {
      const target = event.target;
      const node = target instanceof Element ? target.closest<HTMLElement>("[data-run]") : null;
      const role = node?.dataset.role;
      if (!node || !role || !(role in XRAY_STYLE)) {
        setAt(null);
        return;
      }
      setAt({
        rect: node.getBoundingClientRect(),
        paragraphIndex: Number(node.dataset.paragraph),
        segmentIndex: Number(node.dataset.segment),
      });
    };
    const clear = () => setAt(null);

    root.addEventListener("mouseover", onPointer);
    root.addEventListener("mouseleave", clear);
    root.addEventListener("focusin", onPointer);
    root.addEventListener("focusout", clear);
    // A rect measured once is wrong the moment the panel scrolls under it, and
    // there is no second hover event to correct it -- so it goes away rather
    // than pointing at the wrong run.
    window.addEventListener("scroll", clear, true);
    return () => {
      root.removeEventListener("mouseover", onPointer);
      root.removeEventListener("mouseleave", clear);
      root.removeEventListener("focusin", onPointer);
      root.removeEventListener("focusout", clear);
      window.removeEventListener("scroll", clear, true);
    };
  }, [container, active]);

  if (!at) return null;
  // Position in the array is the paragraph index by construction: `walkParagraphs`
  // numbers them as it pushes them, in the same document order the backend does.
  const segment = paragraphs[at.paragraphIndex]?.block.segments[at.segmentIndex];
  if (!segment) return null;
  const style = XRAY_STYLE[segment.role];
  if (!style) return null;

  const object = objectForRun(at.paragraphIndex, segment, objectIndex);
  const expression = object?.object_type === "CONDITION"
    ? String(object.expression ?? "").trim() : "";
  // The author's own sentence, quoted back. Deliberately not run through
  // `plainly()`: that rewrites our vocabulary on the way to the screen, and this
  // string is not ours -- it is a line out of the customer's template, and
  // rewording it would misquote the document.
  const compiledFrom = String(object?.compiled_from ?? "").trim();

  // Above the run where there is room for it, below where there is not.
  const above = at.rect.top > 180;
  const left = clampCentre(at.rect, 180);

  return createPortal(
    <div
      className="pointer-events-none fixed z-50"
      style={{
        left,
        top: above ? at.rect.top - 10 : at.rect.bottom + 10,
        transform: `translate(-50%, ${above ? "-100%" : "0%"})`,
      }}
    >
      <motion.div
        initial={{ opacity: 0, y: above ? 4 : -4 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: DUR.micro, ease: EASE_OUT }}
        className="max-w-[22rem] rounded-lg surface-glass px-3 py-2 shadow-lg"
      >
        <p className="flex items-center gap-1.5 text-xs font-medium">
          <span className={cn("h-1.5 w-1.5 rounded-full", style.dot)} />
          {ROLE_STYLE[segment.role].label}
        </p>

        {segment.role === "mergefield" && segment.code && (
          <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
            {segment.code}
          </p>
        )}

        {object?.object_id && (
          <p className="mt-1.5 break-all font-mono text-[11px] text-muted-foreground">
            {String(object.object_id)}
          </p>
        )}

        {expression && (
          <p className="mt-1 break-all font-mono text-[11px] text-foreground/90">{expression}</p>
        )}

        {compiledFrom && (
          <p className="mt-1.5 border-t border-border/60 pt-1.5 text-[11px] leading-snug text-muted-foreground">
            Read from: &ldquo;{compiledFrom}&rdquo;
          </p>
        )}

        {segment.emit === false && (
          <p className="mt-1.5 text-[11px] leading-snug text-muted-foreground">
            Left out of the published template.
          </p>
        )}
      </motion.div>
    </div>,
    document.body,
  );
}


/**
 * The three things you can do to a run, at the run.
 *
 * Every one of them is the inspector's own control calling the inspector's own
 * `update()` -- this relocates the reach, it does not add a second way to change
 * the document. There is no insert, split or delete here because there is none
 * anywhere: the pipeline addresses text by (paragraph, span) and an edit changes
 * one run, so a toolbar offering to split one would be offering an operation the
 * engine has no shape for.
 *
 * The anchor is found by data attribute rather than remembered from the click,
 * because a rect captured once is wrong the moment the panel scrolls -- and
 * because the run may re-render underneath it.
 */
function RunToolbar({ container, anchorKey, segment, onRole, onEmit, onDismiss }: {
  container: RefObject<HTMLDivElement | null>;
  anchorKey: string;
  segment: BlueprintSegment;
  onRole: (role: SegmentRole) => void;
  onEmit: (leaveOut: boolean) => void;
  onDismiss: () => void;
}) {
  const [rect, setRect] = useState<DOMRect | null>(null);

  useEffect(() => {
    let frame = 0;
    const measure = () => {
      frame = 0;
      const root = container.current;
      const node = root?.querySelector<HTMLElement>(`[data-run="${CSS.escape(anchorKey)}"]`);
      if (!root || !node) {
        setRect(null);
        return;
      }
      const bounds = node.getBoundingClientRect();
      const panel = root.getBoundingClientRect();
      // Scrolled out of the panel: the run is still selected and the inspector
      // still holds it, but there is nothing on screen for this to point at.
      const visible = bounds.bottom > panel.top + 4 && bounds.top < panel.bottom - 4;
      setRect(visible ? bounds : null);
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(measure);
    };
    measure();
    window.addEventListener("scroll", schedule, true);
    window.addEventListener("resize", schedule);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      window.removeEventListener("scroll", schedule, true);
      window.removeEventListener("resize", schedule);
    };
  }, [container, anchorKey, segment]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onDismiss();
    };
    const onDown = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      // Clicking another run moves the toolbar rather than dismissing it, so a
      // run is not "outside".
      if (target.closest("[data-run]") || target.closest("[data-run-toolbar]")) return;
      onDismiss();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [onDismiss]);

  if (!rect) return null;

  // Preserved from the inspector: a merge field and a hyperlink are read out of
  // the document's own structure, not classified by colour, so re-roling one
  // would be describing it as something it is not.
  const locked = segment.role === "mergefield" || segment.role === "hyperlink";
  const above = rect.top > 140;
  // Wide enough for the row with the emit toggle on it; narrower windows wrap
  // it rather than push it off the edge.
  const left = clampCentre(rect, 280);

  return createPortal(
    <div
      data-run-toolbar=""
      className="fixed z-50"
      style={{
        left,
        top: above ? rect.top - 12 : rect.bottom + 12,
        transform: `translate(-50%, ${above ? "-100%" : "0%"})`,
      }}
    >
      <motion.div
        initial={{ opacity: 0, y: above ? 4 : -4 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: DUR.micro, ease: EASE_OUT }}
        className="flex max-w-[calc(100vw-2rem)] flex-wrap items-center gap-1 rounded-lg surface-glass p-1"
      >
        {(["static", "placeholder", "instruction"] as SegmentRole[]).map((role) => (
          <button
            key={role}
            type="button"
            disabled={locked}
            onClick={() => onRole(role)}
            className={cn(
              "rounded-md px-2 py-1 text-xs font-medium",
              "transition-[scale,color,background-color] duration-150 ease-out active:scale-[0.98]",
              "disabled:cursor-not-allowed disabled:opacity-40",
              segment.role === role
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-accent hover:text-foreground",
            )}
          >
            {ROLE_STYLE[role].label}
          </button>
        ))}

        {segment.role === "instruction" && (
          <>
            <span aria-hidden className="mx-0.5 h-4 w-px bg-border" />
            <button
              type="button"
              aria-pressed={segment.emit === false}
              onClick={() => onEmit(segment.emit !== false)}
              title="Keep this instruction out of the file that ships. The reading marks the instructions it recognised; you decide whether each one was written for you or for the reader."
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium",
                "transition-[scale,color,background-color] duration-150 ease-out active:scale-[0.98]",
                segment.emit === false
                  ? "bg-run-instruction/15 text-run-instruction"
                  : "text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              <EyeOff className="h-3.5 w-3.5" /> Leave out of the published template
            </button>
          </>
        )}
      </motion.div>
    </div>,
    document.body,
  );
}


/**
 * Remove this template, under the chat that edits it.
 *
 * Two outcomes, and the difference is worth being honest about rather than
 * hiding behind one word. A template nothing has been published from is deleted:
 * it comes off the list and the retention sweep destroys the file later. One
 * that *has* been published is refused, because letters already sent name the
 * version they came from -- and for those the server offers an archive, which
 * takes it off the list and leaves everything generated from it alone.
 *
 * The refusal is the common case now, not the rare one: compiling approves its
 * own reading, so almost every template that has been read has an approved
 * manifest. So rather than showing the user a 409 and stopping, this asks the
 * question again with the honest answer attached.
 */
function DeleteTemplatePanel({ blueprintId, name, open, onOpenChange, onGone }: {
  blueprintId: string;
  name: string;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onGone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  /** Set when the server refused the delete because something was published
   *  from this template. Carries the reason it gave. */
  const [inUse, setInUse] = useState<string | null>(null);

  const run = async (archive: boolean) => {
    setBusy(true);
    try {
      if (archive) {
        await api.archiveBlueprint(blueprintId);
        toast.success("Template archived", {
          description: `${name} is off the list. Everything generated from it is unchanged.`,
        });
      } else {
        await api.deleteBlueprint(blueprintId);
        toast.success("Template deleted", { description: name });
      }
      onOpenChange(false);
      onGone();
    } catch (e: any) {
      if (!archive && e?.code === "BLUEPRINT_IN_USE") {
        // Not an error to report and stop on -- it is the answer to a question
        // the user has not been asked yet.
        setInUse(e?.message ?? "Something has been published from this template.");
        return;
      }
      toast.error(archive ? "Could not archive this template" : "Could not delete this template", {
        description: e?.message ?? String(e),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-xl border border-destructive/25 bg-destructive/5 p-4">
      <p className="mb-1 flex items-center gap-1.5 text-xs font-medium text-destructive">
        <Trash2 className="h-3.5 w-3.5" /> Remove this template
      </p>
      <p className="mb-3 text-xs text-muted-foreground">
        Takes it off the Templates list. Anything already generated from it is kept.
      </p>
      <Button variant="outline" size="sm"
              className="w-full gap-2 border-destructive/40 text-destructive hover:bg-destructive/10"
              onClick={() => { setInUse(null); onOpenChange(true); }}>
        <Trash2 className="h-3.5 w-3.5" /> Delete template
      </Button>

      <AlertDialog open={open} onOpenChange={(o) => { if (!busy) { onOpenChange(o); if (!o) setInUse(null); } }}>
        <AlertDialogContent className="border-border bg-surface">
          <AlertDialogHeader>
            <AlertDialogTitle>
              {inUse ? `Archive “${name}” instead?` : `Delete “${name}”?`}
            </AlertDialogTitle>
            <AlertDialogDescription className="whitespace-pre-line">
              {inUse
                ? `${inUse}\n\nArchiving takes it off the Templates list and changes nothing else: `
                  + "the template, its versions and every document generated from it stay exactly "
                  + "as they are. Documents already sent keep working."
                : "This removes the template from the list. Anything already compiled or generated "
                  + "from it is kept, and the file itself is destroyed later by the retention sweep, "
                  + "on the schedule your organisation set."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={busy}
              className={cn("bg-destructive text-destructive-foreground hover:bg-destructive/90")}
              // Radix closes on action click; the dialog stays up while the
              // request is in flight so the disabled state is visible, a second
              // click cannot fire it, and the refusal above can replace the
              // question in place rather than after a close and a reopen.
              onClick={(e) => { e.preventDefault(); void run(inUse != null); }}
            >
              {busy ? "Working…" : inUse ? "Archive it" : "Delete template"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}


/**
 * Change the template by asking, or ask what it does.
 *
 * The model never writes the document. It proposes typed operations, the server
 * has already run them through the same guards a hand edit meets, and what is
 * shown here is what *would* happen -- including which suggestions were refused
 * and why. Nothing is saved until Apply.
 *
 * The mode is a switch rather than something inferred from the wording. Guessing
 * between "explain this condition" and "change this condition" is the kind of
 * guess that edits a legal template by accident.
 */
function CopilotPanel({ blueprintId, versionNo, onApplied }: {
  blueprintId: string; versionNo: number; onApplied: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"author" | "explain">("author");
  const [message, setMessage] = useState("");
  const [asking, setAsking] = useState(false);
  const [proposal, setProposal] = useState<any>(null);

  const ask = async () => {
    if (!message.trim()) return;
    setAsking(true);
    setProposal(null);
    try {
      setProposal(await api.blueprintCopilot(blueprintId, { message: message.trim(), mode }));
    } catch (e: any) {
      toast.error(
        e?.code === "LLM_NOT_CONFIGURED" ? "No language model is configured" : "Could not ask",
        { description: e?.message ?? String(e), duration: 8000 });
    } finally { setAsking(false); }
  };

  const apply = async () => {
    setAsking(true);
    try {
      await api.applyBlueprintOperations(blueprintId, {
        ops: proposal.operations,
        expected_version_no: versionNo,
        change_summary: message.trim().slice(0, 120),
      });
      toast.success("Applied");
      setProposal(null);
      setMessage("");
      await onApplied();
    } catch (e: any) {
      toast.error("Could not apply", { description: e?.message ?? String(e) });
    } finally { setAsking(false); }
  };

  if (!open) {
    return (
      <Button variant="outline" className="w-full gap-2" onClick={() => setOpen(true)}>
        <MessageSquare className="h-4 w-4" /> Ask about this template
      </Button>
    );
  }

  return (
    <div className="rounded-xl surface-raised p-4">
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs uppercase tracking-wider text-muted-foreground">Co-pilot</p>
        <button onClick={() => setOpen(false)} className="text-muted-foreground hover:text-foreground">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="mb-2 flex gap-1.5">
        {(["author", "explain"] as const).map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            className={cn("rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
                          mode === m ? "border-primary bg-primary text-primary-foreground"
                                     : "border-border hover:bg-accent")}
          >
            {m === "author" ? "Change it" : "Explain it"}
          </button>
        ))}
      </div>

      <Textarea
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        rows={3}
        placeholder={mode === "author"
          ? "Rename the salary field to something the HR team would recognise"
          : "Why would a part-time colleague's letter lose the bonus paragraph?"}
        className="text-sm"
      />
      <Button size="sm" onClick={ask} disabled={asking || !message.trim()} className="mt-2 w-full gap-1.5">
        {asking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
        {mode === "author" ? "Propose changes" : "Ask"}
      </Button>

      {proposal?.mode === "explain" && (
        <div className="mt-3 space-y-2">
          {proposal.answers?.map((a: any, i: number) => (
            <div key={i} className="rounded-lg border border-border bg-muted/40 p-2.5 text-xs">
              <p className="leading-snug">{a.answer}</p>
              {a.paragraph_indices?.length > 0 && (
                <p className="mt-1 text-[11px] text-muted-foreground">
                  paragraphs {a.paragraph_indices.join(", ")}
                </p>
              )}
            </div>
          ))}
          {!proposal.answers?.length && (
            <p className="text-xs text-muted-foreground">
              Nothing in the template or its lineage answers that.
            </p>
          )}
        </div>
      )}

      {proposal?.mode === "author" && (
        <div className="mt-3 space-y-2">
          {proposal.operations?.length > 0 ? (
            <>
              <p className="text-xs text-muted-foreground">
                {proposal.operations.length} change{proposal.operations.length === 1 ? "" : "s"} would
                be made. Nothing is saved until you apply them.
              </p>
              {proposal.operations.map((op: any, i: number) => (
                <div key={i} className="rounded-lg border border-border bg-muted/40 p-2.5 text-xs">
                  <div className="font-mono text-[10px] uppercase opacity-70">{op.op}</div>
                  {op.reason && <div className="mt-0.5 leading-snug">{op.reason}</div>}
                </div>
              ))}
              <Button size="sm" onClick={apply} disabled={asking} className="w-full gap-1.5">
                <CheckCircle2 className="h-3.5 w-3.5" /> Apply {proposal.operations.length}
              </Button>
            </>
          ) : (
            <p className="text-xs text-muted-foreground">
              {proposal.verdict === "need_more_context"
                ? "Not enough to go on — it asked rather than guessed."
                : "No change was proposed."}
            </p>
          )}

          {proposal.questions?.map((q: string, i: number) => (
            <p key={i} className="text-xs italic text-muted-foreground">“{q}”</p>
          ))}

          {proposal.rejected?.length > 0 && (
            <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-2.5 text-xs text-amber-500">
              <p className="mb-1 font-medium">
                {proposal.rejected.length} suggestion{proposal.rejected.length === 1 ? "" : "s"} refused
              </p>
              {proposal.rejected.slice(0, 3).map((r: any, i: number) => (
                <p key={i} className="leading-snug opacity-90">{r.reason}</p>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
