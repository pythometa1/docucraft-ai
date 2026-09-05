/**
 * ⌘K — the palette the header has been promising since it was written.
 *
 * There was a search input in the header with no handler and a ⌘K badge beside
 * it, and `cmdk` plus `ui/command.tsx` were already installed and imported by
 * nothing. So the parts were all here; what was missing was the twenty lines
 * that ask the server.
 *
 * It searches exactly what the API can search and nothing more:
 *
 *  - **Projects**, by name, through `listProjects` — debounced, and guarded
 *    against out-of-order responses.
 *  - **Templates**, matched in the browser against the org's blueprints.
 *    `listBlueprints` takes no query of its own, so this is the same
 *    client-side match the templates screen already does, and it is fetched
 *    once per opening rather than re-asked on every keystroke for bytes that
 *    cannot change between them.
 *
 * There is deliberately no document search. No endpoint offers full-text search
 * over letter bodies, and a box that implies one would be promising something
 * the server cannot deliver — promising less and delivering it is the whole
 * point of the control.
 *
 * Under the results sit every destination the sidebar has, not the three it
 * used to list, so ⌘K is a complete way to move through the app rather than a
 * shortcut to a fraction of it.
 *
 * The dialog is composed from the Radix primitives instead of the shared
 * `CommandDialog`, which hard-codes an opaque panel, an opaque black scrim, a
 * close button that lands on top of the input, and a CSS enter animation — four
 * things that would each fight the glass surface and the spring below. Nothing
 * about the shared wrapper changes; other dialogs keep using it.
 */

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useNavigate } from "@tanstack/react-router";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { AnimatePresence, motion } from "framer-motion";
import {
  BarChart3, BookOpen, ClipboardCheck, FileText, FolderKanban, Gauge,
  LayoutTemplate, MessageSquare, ReceiptText, Settings, Shield, Users,
} from "lucide-react";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { DUR, EASE_OUT, SPRING_POP, staggerDelay, useReducedMotionFlag } from "@/components/motion";
import { SkeletonBar } from "@/components/skeletons";
import {
  Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
} from "@/components/ui/command";
import type { Blueprint } from "@/lib/types";

type Hit =
  | { kind: "project"; id: string; name: string; sub: string }
  | { kind: "template"; id: string; name: string; sub: string };

/** Every route the sidebar offers, in the sidebar's order and with the sidebar's
 *  words. Two lists of destinations that disagree about what a screen is called
 *  is how a person ends up unable to find, from the keyboard, the thing they can
 *  see in the nav. `as const` per entry rather than over the array, so the router
 *  still resolves each `to` against its real route. */
const DESTINATIONS = [
  { label: "Projects", to: "/dashboard" as const, icon: FolderKanban },
  { label: "Chat", to: "/chat" as const, icon: MessageSquare },
  { label: "Templates", to: "/templates" as const, icon: FileText },
  { label: "Invoices", to: "/invoices" as const, icon: ReceiptText },
  { label: "Review", to: "/review" as const, icon: ClipboardCheck },
  { label: "Analytics", to: "/analytics" as const, icon: BarChart3 },
  { label: "Quality", to: "/quality" as const, icon: Gauge },
  { label: "Team", to: "/team" as const, icon: Users },
  { label: "Audit Log", to: "/audit-log" as const, icon: Shield },
  { label: "Settings", to: "/settings" as const, icon: Settings },
  { label: "Docs", to: "/docs" as const, icon: BookOpen },
];

/** The template's state in the words the templates screen uses. Falls through to
 *  whatever the row actually holds rather than showing nothing, because a status
 *  we have not met is still information. */
const TEMPLATE_STATUS: Record<string, string> = {
  published: "Published",
  draft: "Draft",
  archived: "Archived",
};

/** Enough templates to be useful, few enough that the palette stays a palette
 *  and not a second templates screen. */
const TEMPLATE_LIMIT = 6;

/* ---------------------------------------------------------------------------
   Class tokens.

   cmdk renders its own elements and labels them with attributes, so the only way
   to reach them is by attribute selector. Doing that here rather than in
   `ui/command.tsx` keeps the shared primitive as every other dialog expects it.
   --------------------------------------------------------------------------- */

/** `bg-transparent` is load-bearing: `Command` paints `bg-popover`, and an
 *  opaque panel inside a glass one is just a panel. */
const SHELL = cn(
  "bg-transparent",
  "[&_[cmdk-input]]:h-12",
  "[&_[cmdk-input-wrapper]]:border-border/70 [&_[cmdk-input-wrapper]]:px-4",
  "[&_[cmdk-group]]:px-2 [&_[cmdk-group]:not(:first-child)]:pt-1",
  "[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-2.5",
  "[&_[cmdk-group-heading]]:text-[10.5px] [&_[cmdk-group-heading]]:font-semibold",
  "[&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-[0.08em]",
  "[&_[cmdk-group-heading]]:text-muted-foreground/80",
);

/** One row shape for all three groups. The ring is on `data-[selected]` because
 *  that — not DOM focus — is where cmdk puts the keyboard cursor; the
 *  `focus-visible` ring is kept for anything that reaches a row by tabbing. */
const ROW = cn(
  "cursor-pointer rounded-lg px-2 py-2 text-sm",
  "transition-[transform,background-color,box-shadow] duration-150 ease-out",
  "active:scale-[0.98]",
  "data-[selected=true]:bg-accent data-[selected=true]:ring-1 data-[selected=true]:ring-brand/35",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/60",
);

const ICON_TILE = "flex h-7 w-7 shrink-0 items-center justify-center rounded-md border";

export function CommandPalette({ open, onOpenChange }: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const navigate = useNavigate();
  const reduced = useReducedMotionFlag();
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [blueprints, setBlueprints] = useState<Blueprint[]>([]);
  const [loading, setLoading] = useState(false);

  // Debounced, and guarded against out-of-order responses. Typing "offer" fires
  // five requests and the third can land after the fifth; without the sequence
  // check the list would settle on results for "off".
  useEffect(() => {
    if (!open) return;
    let live = true;
    setLoading(true);
    const timer = setTimeout(() => {
      api.listProjects(query || undefined, { limit: 8 })
        .then(async (res) => {
          if (!live) return;
          const projects: Hit[] = (res.items ?? []).map((p: any) => ({
            kind: "project" as const,
            id: p.id,
            name: p.name,
            sub: [p.region, p.function].filter(Boolean).join(" · "),
          }));
          setHits(projects);
        })
        .catch(() => { if (live) setHits([]); })
        .finally(() => { if (live) setLoading(false); });
    }, 200);
    return () => { live = false; clearTimeout(timer); };
  }, [query, open]);

  // Templates arrive whole and unfiltered — the endpoint has no query parameter
  // — so this is asked once per opening and matched in the browser. A failure is
  // swallowed to an empty list rather than surfaced: the palette's other half
  // still works, and a toast fired by a keyboard shortcut somebody may have hit
  // by accident is noise.
  useEffect(() => {
    if (!open) return;
    let live = true;
    api.listBlueprints()
      .then((res) => { if (live) setBlueprints(res.items ?? []); })
      .catch(() => { if (live) setBlueprints([]); });
    return () => { live = false; };
  }, [open]);

  // Cleared on close rather than on open, so reopening does not flash the
  // previous search before the new one lands.
  useEffect(() => { if (!open) { setQuery(""); setHits([]); } }, [open]);

  const projects = useMemo(() => hits.filter((h) => h.kind === "project"), [hits]);

  /** Most recently touched first, so an empty query opens on the templates
   *  somebody is actually working on. `updated_at` is declared a string but comes
   *  back null on rows written before the column existed, and `new Date(null)`
   *  sorts as NaN — undated rows go last rather than to the top. */
  const templates = useMemo<Hit[]>(() => {
    const needle = query.trim().toLowerCase();
    const stamp = (v: string | null | undefined) => {
      const t = v ? new Date(v).getTime() : NaN;
      return Number.isNaN(t) ? 0 : t;
    };
    return blueprints
      .filter((b) => !needle || b.name.toLowerCase().includes(needle))
      .slice()
      .sort((a, b) => stamp(b.updated_at) - stamp(a.updated_at))
      .slice(0, TEMPLATE_LIMIT)
      .map((b) => ({
        kind: "template" as const,
        id: b.id,
        name: b.name,
        sub: [
          TEMPLATE_STATUS[b.status] ?? b.status,
          b.version_no != null ? `v${b.version_no}` : null,
        ].filter(Boolean).join(" · "),
      }));
  }, [blueprints, query]);

  const go = (to: () => void) => { onOpenChange(false); to(); };

  // One running index across all three groups, so the stagger reads as a single
  // list settling rather than three lists racing each other.
  let row = 0;

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <AnimatePresence>
        {open && (
          <DialogPrimitive.Portal key="command-palette" forceMount>
            {/* The scrim blurs rather than dims. Dimming hides the page; blurring
                keeps it legible as the place you are about to move within. */}
            <DialogPrimitive.Overlay asChild forceMount>
              <motion.div
                className="fixed inset-0 z-50 bg-background/55 backdrop-blur-[6px]"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: reduced ? 0 : DUR.micro, ease: EASE_OUT }}
              />
            </DialogPrimitive.Overlay>

            <DialogPrimitive.Content asChild forceMount aria-describedby={undefined}>
              {/* Centred with a transform in the animation rather than with
                  `-translate-x-1/2`: framer writes `transform` inline, which would
                  overwrite the class and drop the panel half a width to the right. */}
              <motion.div
                className={cn(
                  "fixed left-1/2 top-[16vh] z-50 w-[calc(100%-2rem)] max-w-xl",
                  "surface-glass overflow-hidden rounded-xl",
                )}
                initial={reduced ? { x: "-50%" } : { opacity: 0, scale: 0.96, y: -8, x: "-50%" }}
                animate={{ opacity: 1, scale: 1, y: 0, x: "-50%" }}
                exit={{ opacity: 0, scale: reduced ? 1 : 0.985, x: "-50%" }}
                transition={reduced ? { duration: 0 } : SPRING_POP}
              >
                <DialogPrimitive.Title className="sr-only">Search</DialogPrimitive.Title>

                <Command className={SHELL}>
                  <CommandInput
                    placeholder="Search projects and templates…"
                    value={query}
                    onValueChange={setQuery}
                  />
                  <CommandList className="max-h-[min(60vh,420px)] px-1 pb-2">
                    {loading && (
                      // Three bars where three rows are about to be, so nothing
                      // jumps when the answer lands. A spinner in the same space
                      // would move everything below it twice.
                      <div className="space-y-3 px-3 py-4" aria-hidden>
                        <SkeletonBar className="h-3 w-2/5" />
                        <SkeletonBar className="h-3 w-3/5" />
                        <SkeletonBar className="h-3 w-1/3" />
                      </div>
                    )}
                    {!loading && <CommandEmpty>Nothing matches that.</CommandEmpty>}

                    {projects.length > 0 && (
                      <CommandGroup heading="Projects">
                        {projects.map((p) => (
                          <CommandItem
                            key={p.id}
                            value={`${p.name} ${p.sub}`}
                            className={ROW}
                            onSelect={() => go(() => navigate({ to: "/projects/$id", params: { id: p.id } }))}
                          >
                            <Row
                              index={row++}
                              reduced={reduced}
                              icon={<FolderKanban className="h-3.5 w-3.5" />}
                              tone="border-brand/35 bg-brand/10 text-brand"
                              name={p.name}
                              sub={p.sub}
                            />
                          </CommandItem>
                        ))}
                      </CommandGroup>
                    )}

                    {templates.length > 0 && (
                      <CommandGroup heading="Templates">
                        {templates.map((t) => (
                          <CommandItem
                            key={t.id}
                            value={`${t.name} ${t.sub}`}
                            className={ROW}
                            onSelect={() => go(() => navigate({
                              to: "/templates/$blueprintId",
                              params: { blueprintId: t.id },
                            }))}
                          >
                            <Row
                              index={row++}
                              reduced={reduced}
                              icon={<LayoutTemplate className="h-3.5 w-3.5" />}
                              tone="border-ai-active/35 bg-ai-active/10 text-ai-active"
                              name={t.name}
                              sub={t.sub}
                            />
                          </CommandItem>
                        ))}
                      </CommandGroup>
                    )}

                    <CommandGroup heading="Go to">
                      {DESTINATIONS.map((item) => (
                        <CommandItem
                          key={item.to}
                          value={item.label}
                          className={ROW}
                          onSelect={() => go(() => navigate({ to: item.to }))}
                        >
                          <Row
                            index={row++}
                            reduced={reduced}
                            icon={<item.icon className="h-3.5 w-3.5" />}
                            tone="border-border bg-surface text-muted-foreground"
                            name={item.label}
                          />
                        </CommandItem>
                      ))}
                    </CommandGroup>
                  </CommandList>

                  {/* Only keys that do something. A hint bar listing a shortcut the
                      palette does not implement is worse than no hint bar. */}
                  <div className="flex items-center gap-3 border-t border-border/70 px-4 py-2 text-[11px] text-muted-foreground">
                    <Hint keys={["↑", "↓"]}>to move</Hint>
                    <Hint keys={["↵"]}>to open</Hint>
                    <Hint keys={["esc"]}>to close</Hint>
                  </div>
                </Command>
              </motion.div>
            </DialogPrimitive.Content>
          </DialogPrimitive.Portal>
        )}
      </AnimatePresence>
    </DialogPrimitive.Root>
  );
}

/** A row's contents. The stagger is 20ms rather than the list default of 30:
 *  these rows re-mount on every keystroke as cmdk filters, and at 30ms a
 *  ten-row list would still be arriving when the next character is typed. */
function Row({ index, icon, tone, name, sub, reduced }: {
  index: number;
  icon: ReactNode;
  tone: string;
  name: string;
  sub?: string;
  /** Passed down rather than read per row: one `matchMedia` listener for the
   *  palette, not one for every result in it. */
  reduced: boolean;
}) {
  const body = (
    <>
      <span className={cn(ICON_TILE, tone)}>{icon}</span>
      <span className="min-w-0 flex-1 truncate">{name}</span>
      {sub && <span className="ml-2 shrink-0 truncate text-xs text-muted-foreground">{sub}</span>}
    </>
  );
  const className = "flex w-full min-w-0 items-center gap-2.5";

  if (reduced) return <span className={className}>{body}</span>;
  return (
    <motion.span
      className={className}
      initial={{ opacity: 0, y: 2 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: DUR.micro, ease: EASE_OUT, delay: staggerDelay(index, 20) }}
    >
      {body}
    </motion.span>
  );
}

function Hint({ keys, children }: { keys: string[]; children: ReactNode }) {
  return (
    <span className="flex items-center gap-1">
      {keys.map((k) => (
        <kbd
          key={k}
          className="rounded border border-border bg-surface px-1.5 py-0.5 font-sans text-[10px] leading-none text-foreground/70"
        >
          {k}
        </kbd>
      ))}
      <span>{children}</span>
    </span>
  );
}
