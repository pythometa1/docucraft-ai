/**
 * Ticking several rows and doing one thing to them.
 *
 * Four lists in this product can be multi-selected, and writing the same
 * selection logic four times is how three of them end up subtly different --
 * one forgets to clear the selection after acting, another counts rows the
 * server would refuse, a third keeps indices instead of ids and deletes the
 * wrong thing after a reload. So it is written once, here.
 *
 * Two rules the whole file exists to enforce:
 *
 * **Selection is by id, never by index.** Every list here is reloaded from the
 * server after an action, and the row that was third before is not necessarily
 * the row that is third afterwards.
 *
 * **What cannot be acted on cannot be ticked.** An approved document is refused
 * by the server, so its checkbox is disabled and says why. Stating the rule
 * before anything is chosen beats confirming a destructive dialog and then being
 * told no.
 */

import { useMemo, useState } from "react";
import { toast } from "sonner";
import { Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { cn } from "@/lib/utils";

export type Selection = {
  /** Ids currently ticked that are still present and still actionable. */
  chosen: string[];
  has: (id: string) => boolean;
  toggle: (id: string) => void;
  /** True only when every actionable row is ticked, and there is at least one. */
  allChosen: boolean;
  setAll: (on: boolean) => void;
  clear: () => void;
  /** Rows the caller said may be acted on, in list order. */
  actionable: any[];
  /** How many rows exist but cannot be acted on. */
  blockedCount: number;
  /** Most the server accepts in one request. */
  limit: number;
  /** True when there are more actionable rows than one request can carry. */
  overLimit: boolean;
};

/**
 * @param items      the full list, in the order it is rendered
 * @param getId      how to read a stable id off a row
 * @param canAct     which rows may be acted on; the rest are not selectable
 */
export function useSelection(
  items: any[],
  getId: (item: any) => string,
  canAct: (item: any) => boolean = () => true,
  /** Most the server will take in one request. Select-all stops here. */
  limit = 50,
): Selection {
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const actionable = useMemo(() => items.filter(canAct), [items, canAct]);
  const actionableIds = useMemo(
    () => new Set(actionable.map(getId)), [actionable, getId]);

  // Intersected with what is currently on screen and actionable, so an id left
  // over from a previous round -- a row since deleted, or since approved by
  // somebody else -- cannot be counted or sent.
  const chosen = useMemo(
    () => [...selected].filter((id) => actionableIds.has(id)),
    [selected, actionableIds]);

  return {
    chosen,
    // Effective selection, not the raw set. A row that stops being actionable
    // while the page is open -- a document somebody else approves, one somebody
    // else deletes -- was still drawn ticked and highlighted while being silently
    // dropped from the count, and because its checkbox is disabled there was no
    // way to untick it. What is drawn now matches what would be sent.
    has: (id) => selected.has(id) && actionableIds.has(id),
    toggle: (id) =>
      setSelected((prev) => {
        const next = new Set(prev);
        if (next.has(id)) next.delete(id); else next.add(id);
        return next;
      }),
    allChosen: chosen.length > 0 && chosen.length === Math.min(actionable.length, limit),
    setAll: (on) =>
      // Capped at what the endpoint will accept. Batch generation produces one
      // document per source row, so a project past the limit is the ordinary
      // case here -- and an uncapped "select all" produced a request the server
      // rejected outright, deleting nothing and explaining it in a toast.
      setSelected(on ? new Set(actionable.slice(0, limit).map(getId)) : new Set()),
    limit,
    overLimit: actionable.length > limit,
    clear: () => setSelected(new Set()),
    actionable,
    blockedCount: items.length - actionable.length,
  };
}

/** The checkbox on a row. `blockedReason` disables it and explains itself. */
export function SelectBox({ id, selection, label, blockedReason }: {
  id: string;
  selection: Selection;
  label: string;
  blockedReason?: string;
}) {
  return (
    <Checkbox
      checked={selection.has(id)}
      disabled={!!blockedReason}
      onCheckedChange={() => selection.toggle(id)}
      aria-label={blockedReason ? `${label} — ${blockedReason}` : `Select ${label}`}
      title={blockedReason}
      onClick={(e) => e.stopPropagation()}
    />
  );
}

/**
 * The toolbar above a selectable list: select-all, a count, and one destructive
 * action.
 *
 * `onDelete` returns the server's own per-row answer, which this reports rather
 * than assuming the request was granted in full. A row can stop being deletable
 * between the page rendering and the button being pressed -- somebody else
 * approves a document, or deletes it first -- and the honest summary is the one
 * that says so.
 */
export function BulkSelectBar({
  selection, noun, pluralNoun, names, blockedNote, onDelete, onDone, idle, extra,
}: {
  selection: Selection;
  noun: string;
  pluralNoun: string;
  /** Names of the chosen rows, for the confirmation to list. */
  names: string[];
  /** What to say about rows that could not be ticked, if any. */
  blockedNote?: string;
  onDelete: (ids: string[]) => Promise<{
    deleted: unknown[];
    refused: { reason: string }[];
  }>;
  onDone: () => Promise<void> | void;
  /** Shown in place of the count when nothing is ticked. */
  idle?: React.ReactNode;
  /** Rendered to the right of the delete button, e.g. a download control. */
  extra?: React.ReactNode;
  description?: string;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const count = selection.chosen.length;
  const word = count === 1 ? noun : pluralNoun;

  const run = async () => {
    setBusy(true);
    try {
      const result = await onDelete(selection.chosen);
      const gone = result.deleted.length;
      const kept = result.refused.length;
      if (gone > 0 && kept === 0) {
        toast.success(`Deleted ${gone} ${gone === 1 ? noun : pluralNoun}`);
      } else if (gone > 0) {
        // Both halves. "Deleted 8" on its own would let the two that were
        // refused pass unnoticed, which is the failure this whole shape exists
        // to avoid.
        toast.warning(`Deleted ${gone}; left ${kept} alone`, {
          description: result.refused[0].reason,
        });
      } else {
        toast.error(`Nothing was deleted`, {
          description: result.refused[0]?.reason ?? "The server refused the request.",
        });
      }
      setOpen(false);
      selection.clear();
    } catch (e: any) {
      toast.error(`Could not delete the selected ${pluralNoun}`, {
        description: e?.message ?? String(e),
      });
    } finally {
      setBusy(false);
      // Outside the try, and reached on failure too. Two reasons.
      //
      // A rejection from `onDone` -- it refetches, and any of those calls can
      // fail -- used to land in the catch above and report "could not delete",
      // contradicting the success toast that had already fired a line earlier.
      //
      // And a failed request is not the same as one that did nothing: the server
      // commits per document, so a dropped connection mid-response leaves an
      // arbitrary prefix genuinely destroyed. Not reloading left those rows on
      // screen, which is the one state the list must never be in.
      try {
        await onDone();
      } catch {
        /* the list is stale rather than wrong; the toast above already spoke */
      }
    }
  };

  return (
    <div className="flex flex-wrap items-center justify-between gap-2 pb-1">
      <div className="flex items-center gap-3">
        {selection.actionable.length > 0 && (
          <label className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
            <Checkbox
              checked={selection.allChosen}
              onCheckedChange={(v) => selection.setAll(v === true)}
              aria-label={`Select all ${pluralNoun}`}
            />
            {count > 0 ? `${count} selected` : "Select all"}
          </label>
        )}
        {selection.overLimit && count > 0 && (
          <span className="text-xs text-muted-foreground">
            of {selection.actionable.length} — {selection.limit} at a time
          </span>
        )}
        {count === 0 && idle}
      </div>

      <div className="flex items-center gap-2">
        {count > 0 && (
          <Button variant="destructive" size="sm" onClick={() => setOpen(true)}>
            <Trash2 className="mr-1.5 h-4 w-4" /> Delete {count}
          </Button>
        )}
        {extra}
      </div>

      <AlertDialog open={open} onOpenChange={(o) => { if (!busy) setOpen(o); }}>
        <AlertDialogContent className="border-border bg-surface">
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {count} {word}?</AlertDialogTitle>
            {/* `whitespace-pre-line` so the names sit on their own lines. */}
            <AlertDialogDescription className="whitespace-pre-line">
              {describe(noun, pluralNoun, count)}
              {names.length > 0 && names.length <= 8 ? `\n\n${names.join("\n")}` : ""}
              {blockedNote ? `\n\n${blockedNote}` : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={busy}
              className={cn("bg-destructive text-destructive-foreground hover:bg-destructive/90")}
              // Radix closes on action click; the dialog has to stay up while the
              // request is in flight so the disabled state is visible and a
              // second click cannot fire it.
              onClick={(e) => { e.preventDefault(); void run(); }}
            >
              {busy ? "Deleting…" : `Delete ${count}`}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

/** What each kind of deletion actually does, in the words of the thing it does
 *  it to. A document is destroyed; the other three are removed from the
 *  workspace and destroyed later by the retention sweep, on the customer's own
 *  schedule. Saying "permanently deletes" for all four would be false for three
 *  of them, and saying "removes" for all four would be false for the one that
 *  unlinks the file from disk. */
function describe(noun: string, pluralNoun: string, count: number): string {
  const these = count === 1 ? `this ${noun}` : `these ${count} ${pluralNoun}`;
  if (noun === "document") {
    return `This permanently deletes ${these}, every version of them, their generation `
      + "lineage and the rendered files on disk. It cannot be undone.";
  }
  if (noun === "project") {
    return `This removes ${these} from the workspace, along with the templates, sources and `
      + "documents inside them. The files themselves are destroyed later by the retention "
      + "sweep, on the schedule your organisation set.";
  }
  return `This removes ${these} from the project. Anything already compiled or generated from `
    + "them is kept, and the uploaded files are destroyed later by the retention sweep.";
}
