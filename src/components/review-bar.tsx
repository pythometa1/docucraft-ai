/**
 * The approve / object / discuss controls for one document version.
 *
 * Extracted because there were two document editors and only one of them had
 * an approve button. Every letter produced by the manifest path -- which is
 * every letter produced by the current pipeline -- opens on the other branch,
 * the one that edits a .docx by its runs. So the controls existed, on a screen
 * those documents never reached, and the documents that did reach it were the
 * legacy HTML drafts.
 *
 * One component rendered on both branches is the only arrangement in which the
 * two cannot drift apart again.
 */

import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  AlertTriangle, CheckCircle2, Clock, Loader2, MessageSquare, RotateCcw, ShieldAlert, XCircle,
} from "lucide-react";
import { api, type DocumentReview, type ReviewComment } from "@/lib/api";
import { useStore } from "@/lib/store";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { ErrorBanner } from "@/components/error-banner";
import { FadeIn, Stagger, StaggerItem } from "@/components/motion";
import { plainly } from "@/components/processing-banner";
import { SkeletonBar } from "@/components/skeletons";
import { cn } from "@/lib/utils";

/** The five states a version can be in, and what each one means to a reader.
 *
 *  `pending_review` and `changes_requested` are two different objections -- the
 *  engine could not work something out, versus a person read it and said no --
 *  and collapsing them would send the reader to the wrong screen.
 *
 *  The colours are the app's intelligence ramp, so a document's status, a
 *  confidence band on the mapping screen and the processing banner all mean the
 *  same thing by the same hue. `changes_requested` is the one that stays
 *  purple: the ramp describes what the engine managed, and this state is the
 *  only one on the list that is entirely a person -- painting it `uncertain`
 *  would make it indistinguishable from `pending_review`, which is exactly the
 *  confusion the two labels exist to prevent.
 *
 *  `draft` keeps `muted-foreground` for its text. `ai-idle` is a deliberately
 *  low-chroma grey and at 12px it does not clear AA against the chip's own
 *  tint; the tint and border carry the idle reading instead. */
const STATUS: Record<string, { label: string; hint: string; icon: any; cls: string }> = {
  draft: {
    label: "Draft",
    hint: "Nothing outstanding. Ready for someone to sign off.",
    icon: Clock,
    cls: "text-muted-foreground bg-ai-idle/10 border-ai-idle/25",
  },
  pending_review: {
    label: "Awaiting a decision",
    hint: "Some values still need your input. Answer the open questions on the Review screen.",
    icon: Clock,
    cls: "text-ai-uncertain bg-ai-uncertain/10 border-ai-uncertain/30",
  },
  changes_requested: {
    label: "Changes requested",
    hint: "Somebody read this and asked for changes.",
    icon: MessageSquare,
    cls: "text-purple bg-purple/10 border-purple/30",
  },
  blocked: {
    label: "Failed checks",
    hint: "This document failed its checks when it was generated and cannot be approved. Fix the template or the spreadsheet row it came from, then generate it again.",
    icon: ShieldAlert,
    cls: "text-ai-blocked bg-ai-blocked/10 border-ai-blocked/30",
  },
  approved: {
    label: "Approved",
    hint: "Signed off.",
    icon: CheckCircle2,
    cls: "text-ai-confident bg-ai-confident/12 border-ai-confident/30",
  },
  final: {
    label: "Final",
    hint: "Signed off and issued.",
    icon: CheckCircle2,
    cls: "text-ai-confident bg-ai-confident/12 border-ai-confident/30",
  },
};

export function StatusChip({ status, reason, className }: {
  status: string | null | undefined;
  reason?: string | null;
  className?: string;
}) {
  const meta = STATUS[status ?? "draft"] ?? STATUS.draft;
  const Icon = meta.icon;
  return (
    <span
      // The reason is the QA checker's own sentence and is written in the
      // engine's vocabulary, so it is scrubbed on its way to the tooltip rather
      // than at the source -- the stored text is what an auditor reads back.
      title={reason ? plainly(reason) : meta.hint}
      className={cn(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium leading-5",
        meta.cls, className,
      )}
    >
      <Icon className="h-3.5 w-3.5 shrink-0" />
      {meta.label}
    </span>
  );
}

type Props = {
  versionId: string;
  /** Called after anything that changes the document's state, so the host can
   *  reload whatever it renders alongside. */
  onChanged?: (status: string) => void;
  /** Saved before approving, on the branch where the editor holds unsaved HTML. */
  beforeApprove?: () => Promise<void>;
  className?: string;
};

export function ReviewBar({ versionId, onChanged, beforeApprove, className }: Props) {
  const capabilities = useStore((s) => s.capabilities);
  const currentUserId = useStore((s) => s.currentUserId);

  const [status, setStatus] = useState<string>("draft");
  const [statusReason, setStatusReason] = useState<string | null>(null);
  const [reviews, setReviews] = useState<DocumentReview[]>([]);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Separate from `busy` on purpose: retrying a read is not a reason to disable
  // Approve, and reusing the action flag here would grey out the controls every
  // time somebody re-checked the status.
  const [reloading, setReloading] = useState(false);
  const [objectOpen, setObjectOpen] = useState(false);
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState("");

  const openReview = reviews.find((r) => r.state === "open") ?? null;
  const canApprove = capabilities.includes("approve_document");
  const canReview = capabilities.includes("review_document");
  const signed = status === "approved" || status === "final";

  const refresh = (): Promise<void> =>
    Promise.all([api.getDocumentVersion(versionId), api.documentReviews(versionId)])
      .then(([version, list]) => {
        setStatus(version.status);
        setStatusReason(version.status_reason);
        setReviews(list.items);
        setLoadError(null);
        onChanged?.(version.status);
      })
      // Held on screen rather than thrown as a toast. A failed read leaves the
      // chip showing the last thing we knew, and a notice that has already faded
      // is no help to somebody deciding whether to trust it.
      .catch((e: any) => {
        setLoadError(e?.message ?? String(e));
      })
      .finally(() => setLoaded(true));

  useEffect(() => { void refresh(); }, [versionId]);

  const run = async (work: () => Promise<unknown>, ok: string): Promise<void> => {
    setBusy(true);
    try {
      await work();
      toast.success(ok);
      await refresh();
    } catch (e: any) {
      // The server's own message, not a generic one: "somebody has asked for
      // changes to this document: the salary is wrong" is the whole reason the
      // refusal is useful. Only the engine's internal words are taken out of it.
      toast.error(plainly(e?.message ?? String(e)));
    } finally {
      setBusy(false);
    }
  };

  // The chip and both buttons, at their real sizes: this bar sits directly above
  // a document, and a one-line spinner that grows into a 32px row pushes the
  // thing the reader is looking at down the page the moment the status lands.
  if (!loaded) {
    return (
      <div
        role="status"
        aria-label="Checking this document's status"
        className={cn("flex flex-wrap items-center gap-2", className)}
      >
        <SkeletonBar className="h-6 w-36 rounded-full" />
        <div className="ml-auto flex items-center gap-2">
          <SkeletonBar className="h-8 w-36 rounded-md" />
          <SkeletonBar className="h-8 w-24 rounded-md" />
        </div>
      </div>
    );
  }

  return (
    <div className={cn("space-y-3", className)}>
      {loadError && (
        <ErrorBanner
          title="Could not check this document's status"
          message="What you see below may be out of date. Nothing has been changed."
          detail={plainly(loadError)}
          onRetry={() => {
            setReloading(true);
            void refresh().finally(() => setReloading(false));
          }}
          retrying={reloading}
        />
      )}

      <div className="flex flex-wrap items-center gap-2">
        <StatusChip status={status} reason={statusReason} />

        {statusReason && !signed && (
          <span className="text-xs text-muted-foreground max-w-md truncate" title={plainly(statusReason)}>
            {plainly(statusReason)}
          </span>
        )}

        <div className="ml-auto flex items-center gap-2">
          {!signed && !openReview && (
            <Button variant="outline" size="sm" onClick={() => { setReason(""); setObjectOpen(true); }}>
              <MessageSquare className="h-4 w-4 mr-1.5" /> Request changes
            </Button>
          )}

          {!signed && (
            <Button
              size="sm"
              className="bg-gradient-brand text-white"
              disabled={busy || !canApprove}
              title={canApprove ? undefined : "Your role cannot approve documents."}
              onClick={() => void run(async () => {
                await beforeApprove?.();
                await api.approveDocumentVersion(versionId);
              }, "Approved")}
            >
              {busy ? <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                    : <CheckCircle2 className="h-4 w-4 mr-1.5" />}
              Approve
            </Button>
          )}

          {signed && (
            <Button
              variant="outline" size="sm" disabled={busy || !canApprove}
              title={canApprove ? undefined : "Your role cannot withdraw an approval."}
              onClick={() => { setReason(""); setRevokeOpen(true); }}
            >
              <RotateCcw className="h-4 w-4 mr-1.5" /> Withdraw approval
            </Button>
          )}
        </div>
      </div>

      {openReview && (
        <ReviewThread
          review={openReview}
          canResolve={canReview}
          isOpener={openReview.requested_by === currentUserId}
          busy={busy}
          onRefresh={refresh}
          onReject={() => { setReason(""); setRejectOpen(true); }}
          onApprove={() => void run(() => api.approveDocumentReview(openReview.id),
                                    "Closed — no change needed")}
          onWithdraw={() => void run(() => api.withdrawDocumentReview(openReview.id), "Withdrawn")}
        />
      )}

      {/* Three dialogs rather than three `prompt()`s, because each of these
          reasons is written into a record somebody will read later. */}
      <ReasonDialog
        open={objectOpen} onOpenChange={setObjectOpen}
        title="What needs to change?"
        description="This goes to whoever fixes the document, and it is what stops the letter being signed in the meantime."
        placeholder="e.g. the base salary is from the 2025 band, it should be the 2026 one"
        confirmLabel="Request changes"
        value={reason} onValueChange={setReason}
        onConfirm={() => void run(async () => {
          await api.requestChanges(versionId, { reason });
          setObjectOpen(false);
        }, "Changes requested")}
      />
      <ReasonDialog
        open={revokeOpen} onOpenChange={setRevokeOpen}
        title="Why is the approval being withdrawn?"
        description="Granting an approval is in the audit record. Withdrawing one has to be too."
        placeholder="e.g. signed against the wrong version of the offer"
        confirmLabel="Withdraw approval"
        destructive
        value={reason} onValueChange={setReason}
        onConfirm={() => void run(async () => {
          await api.revokeDocumentVersion(versionId, { reason });
          setRevokeOpen(false);
        }, "Approval withdrawn")}
      />
      <ReasonDialog
        open={rejectOpen} onOpenChange={setRejectOpen}
        title="What still needs to change?"
        description="It is what whoever fixes this will read."
        placeholder="e.g. use the 2026 band and re-check the pro-rata"
        confirmLabel="Confirm changes needed"
        destructive
        value={reason} onValueChange={setReason}
        onConfirm={() => void run(async () => {
          if (openReview) await api.rejectDocumentReview(openReview.id, { note: reason });
          setRejectOpen(false);
        }, "Recorded")}
      />
    </div>
  );
}

function ReviewThread({
  review, canResolve, isOpener, busy, onRefresh, onApprove, onReject, onWithdraw,
}: {
  review: DocumentReview;
  canResolve: boolean;
  isOpener: boolean;
  busy: boolean;
  onRefresh: () => Promise<void> | void;
  onApprove: () => void;
  onReject: () => void;
  onWithdraw: () => void;
}) {
  const [comments, setComments] = useState<ReviewComment[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [blocker, setBlocker] = useState<string | null>(null);

  useEffect(() => {
    api.documentReview(review.id)
      .then((full) => {
        setComments(full.comments ?? []);
        // The server's own answer to "may I close this", so the disabled button
        // explains itself instead of the reviewer discovering the four-eyes rule
        // as a 403 after typing a rejection note.
        setBlocker(full.can_resolve ? null : (full.cannot_resolve_reason ?? null));
      })
      .catch(() => setComments([]));
  }, [review.id]);

  const send = async () => {
    if (!draft.trim()) return;
    setSending(true);
    try {
      const added = await api.addReviewComment(review.id, { body: draft });
      setComments((c) => [...c, added]);
      setDraft("");
    } catch (e: any) {
      toast.error("Could not add that comment", { description: plainly(e?.message ?? String(e)) });
    } finally {
      setSending(false);
    }
  };

  return (
    // Purple, like the status chip it belongs to: on this screen that colour
    // means a person is in the loop, which is not one of the things the engine
    // can be.
    <FadeIn className="rounded-lg border border-purple/30 bg-purple/5 p-4 space-y-3">
      <div className="flex items-start gap-2">
        <AlertTriangle className="h-4 w-4 mt-0.5 text-purple shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium">{review.title || "Changes requested"}</div>
          <p className="text-sm text-muted-foreground mt-0.5">{review.reason}</p>
          <div className="text-xs text-muted-foreground mt-1">
            Raised by {review.requested_by_name ?? "someone"}
            {review.assigned_to_name ? ` · assigned to ${review.assigned_to_name}` : ""}
          </div>
        </div>
      </div>

      {comments.length > 0 && (
        <Stagger className="space-y-2 pl-6">
          {comments.map((c, i) => (
            <StaggerItem key={c.id} index={i} className="rounded-md border border-border bg-card px-3 py-2">
              <div className="text-xs text-muted-foreground mb-0.5">{c.author_name ?? "Someone"}</div>
              {c.quoted_text && (
                // What the run said when the remark was written. Without it,
                // "this figure is wrong" is worthless the moment somebody
                // changes the figure.
                <blockquote className="border-l-2 border-purple/40 pl-2 mb-1 text-xs italic text-muted-foreground">
                  {c.quoted_text}
                  {c.paragraph_index != null && (
                    <span className="not-italic"> · ¶{c.paragraph_index}</span>
                  )}
                </blockquote>
              )}
              <div className="text-sm whitespace-pre-wrap">{c.body}</div>
            </StaggerItem>
          ))}
        </Stagger>
      )}

      <div className="pl-6 flex flex-col gap-2">
        <Textarea
          rows={2} value={draft} onChange={(e) => setDraft(e.target.value)}
          placeholder="Add to the discussion…" className="text-sm"
        />
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" onClick={send} disabled={sending || !draft.trim()}>
            {sending ? <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                     : <MessageSquare className="h-4 w-4 mr-1.5" />}
            Comment
          </Button>

          <div className="ml-auto flex items-center gap-2">
            {isOpener && (
              <Button variant="ghost" size="sm" disabled={busy} onClick={onWithdraw}>
                Withdraw
              </Button>
            )}
            {canResolve && (
              <>
                <Button
                  variant="outline" size="sm" disabled={busy || blocker != null}
                  title={blocker ? plainly(blocker) : undefined} onClick={onReject}
                >
                  <XCircle className="h-4 w-4 mr-1.5" /> Changes still needed
                </Button>
                <Button
                  size="sm" disabled={busy || blocker != null}
                  title={blocker ? plainly(blocker) : undefined} onClick={onApprove}
                >
                  <CheckCircle2 className="h-4 w-4 mr-1.5" /> Looks right
                </Button>
              </>
            )}
          </div>
        </div>
        {blocker && <p className="text-xs text-muted-foreground">{plainly(blocker)}</p>}
      </div>
    </FadeIn>
  );
}

export function ReasonDialog({
  open, onOpenChange, title, description, placeholder, confirmLabel, value, onValueChange,
  onConfirm, destructive,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  placeholder: string;
  confirmLabel: string;
  value: string;
  onValueChange: (value: string) => void;
  onConfirm: () => void;
  destructive?: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <Textarea
          rows={4} autoFocus value={value} placeholder={placeholder}
          onChange={(e) => onValueChange(e.target.value)}
        />
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button
            variant={destructive ? "destructive" : "default"}
            // Required, and disabled rather than refused: the server returns 400
            // for an empty reason, and letting the button be pressed only to
            // bounce is worse than not offering it.
            disabled={!value.trim()}
            onClick={onConfirm}
          >
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
