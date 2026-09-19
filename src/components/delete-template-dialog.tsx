import { useEffect, useState } from "react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { plainly } from "@/components/processing-banner";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { cn } from "@/lib/utils";

/**
 * Remove one template -- shared by the Templates list and the template editor,
 * so the two places ask the same question and give the same honest answer.
 *
 * Two outcomes. A template nothing has been published from is deleted: it comes
 * off the list and the retention sweep destroys the file later. One that *has*
 * been published is refused (`BLUEPRINT_IN_USE`), because letters already sent
 * name the version they came from -- and for those the server offers an archive,
 * which takes it off the list and leaves everything generated from it alone.
 *
 * The refusal is the common case, not the rare one: compiling approves its own
 * reading, so almost every template that has been read has an approved manifest.
 * So rather than showing a 409 and stopping, the dialog asks again with the
 * honest answer attached.
 */
export function DeleteTemplateDialog({ blueprintId, name, open, onOpenChange, onGone }: {
  blueprintId: string;
  name: string;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** Called once the template is off the list (deleted or archived). */
  onGone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  /** Set when the server refused the delete because something was published
   *  from this template. Carries the reason it gave. */
  const [inUse, setInUse] = useState<string | null>(null);

  // Every opening starts from the plain question, even for the same template.
  useEffect(() => { if (open) setInUse(null); }, [open, blueprintId]);

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
        setInUse(plainly(e?.message ?? "Something has been published from this template."));
        return;
      }
      toast.error(archive ? "Could not archive this template" : "Could not delete this template", {
        description: plainly(e?.message ?? String(e)),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <AlertDialog open={open} onOpenChange={(o) => { if (!busy) onOpenChange(o); }}>
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
              : "This removes the template from the list. Anything already created from it is "
                + "kept, and the file itself is deleted on your organisation's retention schedule."}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            disabled={busy}
            className={cn("bg-destructive text-destructive-foreground hover:bg-destructive/90")}
            // Radix closes on action click; the dialog stays up while the request
            // is in flight so the disabled state is visible, a second click cannot
            // fire it, and the refusal above can replace the question in place
            // rather than after a close and a reopen.
            onClick={(e) => { e.preventDefault(); void run(inUse != null); }}
          >
            {busy ? "Working…" : inUse ? "Archive it" : "Delete template"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
