/**
 * One inbox over two queues.
 *
 * `ReviewTask` is the *engine* saying it could not decide something -- a
 * calculation that needs signing off, a condition whose inputs are ambiguous.
 * Every row is written by the batch runner and nothing a person does creates
 * one. A `DocumentReview` is the opposite: somebody read the finished letter and
 * said no.
 *
 * They are genuinely different questions -- "what should this number be" versus
 * "is this letter right" -- but they land on the same desk, and a person with
 * two queues checks one. So both are listed here, ranked by the server, and the
 * detail pane switches on which kind was picked.
 */

import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import {
  CheckCircle2, ClipboardCheck, FileText, Loader2, MessageSquare, Pencil, ShieldAlert, X,
} from "lucide-react";
import { api, type DocumentReview, type QueueItem, type ReviewComment, type ReviewTask } from "@/lib/api";
import { useStore } from "@/lib/store";
import { StatusChip, ReasonDialog } from "@/components/review-bar";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/_app/review")({
  head: () => ({
    meta: [
      { title: "Review queue — DocuMind AI" },
      { name: "description", content: "Everything waiting for a person: documents somebody objected to, and values the engine would not guess." },
    ],
  }),
  component: ReviewQueue,
});

const KIND_TONE: Record<string, string> = {
  calculation: "text-purple border-purple/40 bg-purple/10",
  condition: "text-warning border-warning/40 bg-warning/10",
  narrative: "text-brand border-brand/40 bg-brand/10",
  binding: "text-info border-info/40 bg-info/10",
};

function ReviewQueue() {
  const [items, setItems] = useState<QueueItem[]>([]);
  const [summary, setSummary] = useState<any>(null);
  const [selected, setSelected] = useState<QueueItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [state, setState] = useState<"open" | "all">("open");
  const [mine, setMine] = useState(false);

  const refresh = () => {
    setLoading(true);
    Promise.all([
      api.reviewQueue({ state, assigned_to: mine ? "me" : undefined }),
      api.reviewSummary(),
    ])
      .then(([queue, s]) => {
        setItems(queue.items);
        setSummary(s);
        setSelected((current) =>
          queue.items.find((i) => i.kind === current?.kind && i.id === current?.id)
          ?? queue.items[0] ?? null);
      })
      .catch((e: any) => toast.error("Could not load the review queue", { description: e?.message ?? String(e) }))
      .finally(() => setLoading(false));
  };

  useEffect(() => { refresh(); }, [state, mine]);

  const counts = useMemo(() => ({
    reviews: items.filter((i) => i.kind === "document_review").length,
    tasks: items.filter((i) => i.kind === "unit_task").length,
  }), [items]);

  return (
    <div className="p-6 lg:p-8 max-w-7xl mx-auto space-y-6">
      <div className="flex items-start justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Review queue</h1>
          <p className="text-sm text-muted-foreground mt-1 max-w-2xl">
            Everything waiting for a person: documents somebody read and objected to, and values the
            engine stopped on rather than guessed. A wrong number in a regulated document is worse
            than a slow one.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 text-xs text-muted-foreground mr-1">
            <Switch checked={mine} onCheckedChange={setMine} /> Assigned to me
          </label>
          {(["open", "all"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setState(f)}
              className={cn(
                "px-3 py-1.5 rounded-md text-xs font-medium border capitalize",
                state === f ? "bg-primary text-primary-foreground border-primary" : "border-border hover:bg-muted",
              )}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {[
          ["Documents objected to", counts.reviews],
          ["Questions from the engine", counts.tasks],
          ["Answered", summary?.resolved ?? "—"],
          ["Human-touch rate", summary && summary.open + summary.resolved > 0
            ? `${Math.round((summary.resolved / (summary.open + summary.resolved)) * 100)}%`
            : "—"],
        ].map(([label, value]) => (
          <div key={String(label)} className="rounded-xl border border-border bg-card p-4">
            <div className="text-xs text-muted-foreground uppercase tracking-wider">{label}</div>
            <div className="text-2xl font-semibold mt-1 tabular-nums">{value}</div>
          </div>
        ))}
      </div>

      <div className="grid lg:grid-cols-[380px_1fr] gap-5">
        <div className="rounded-xl border border-border bg-card overflow-hidden flex flex-col max-h-[72vh]">
          <div className="px-4 py-3 border-b border-border text-xs uppercase tracking-wider text-muted-foreground">
            {items.length} item{items.length === 1 ? "" : "s"}
          </div>
          <div className="flex-1 overflow-auto divide-y divide-border">
            {loading && (
              <div className="p-6 text-sm text-muted-foreground flex items-center gap-2">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading…
              </div>
            )}
            {!loading && items.length === 0 && (
              <div className="p-8 text-center">
                <CheckCircle2 className="h-8 w-8 text-success mx-auto mb-3" />
                <div className="font-medium">Nothing waiting</div>
                <p className="text-sm text-muted-foreground mt-1">
                  Every document accepted and every unit resolved on its own.
                </p>
              </div>
            )}
            {items.map((item) => (
              <button
                key={`${item.kind}:${item.id}`}
                onClick={() => setSelected(item)}
                className={cn(
                  "w-full text-left px-4 py-3 hover:bg-accent/40",
                  selected?.kind === item.kind && selected?.id === item.id && "bg-accent/60",
                )}
              >
                <div className="flex items-center gap-2 mb-1 flex-wrap">
                  {item.kind === "document_review" ? (
                    <Badge variant="outline" className="text-[10px] text-purple border-purple/40 bg-purple/10">
                      <MessageSquare className="h-3 w-3 mr-1" /> document
                    </Badge>
                  ) : (
                    <Badge variant="outline" className={cn("text-[10px]", KIND_TONE[item.task_kind ?? ""])}>
                      {item.task_kind}
                    </Badge>
                  )}
                  {/* The document's own state, so a blocked letter does not look
                      identical to a clean one in the list. */}
                  {item.document_status && item.document_status !== "draft" && (
                    <StatusChip status={item.document_status} className="text-[10px] px-2 py-0" />
                  )}
                  {item.unit_id && (
                    <code className="text-[11px] font-mono text-muted-foreground truncate">{item.unit_id}</code>
                  )}
                </div>
                <div className="text-sm line-clamp-2">{item.title}</div>
                {item.requested_by_name && (
                  <div className="text-[11px] text-muted-foreground mt-1">
                    Raised by {item.requested_by_name}
                  </div>
                )}
              </button>
            ))}
          </div>
        </div>

        {selected == null ? (
          <div className="rounded-xl border border-dashed border-border p-10 text-center text-sm text-muted-foreground">
            Pick something from the queue.
          </div>
        ) : selected.kind === "document_review" ? (
          <ReviewDetail key={selected.id} reviewId={selected.id} onDone={refresh} />
        ) : (
          <TaskDetailLoader key={selected.id} taskId={selected.id} onDone={refresh} />
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------ a person objected */

function ReviewDetail({ reviewId, onDone }: { reviewId: string; onDone: () => void }) {
  const capabilities = useStore((s) => s.capabilities);
  const currentUserId = useStore((s) => s.currentUserId);

  const [review, setReview] = useState<DocumentReview | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [note, setNote] = useState("");

  const load = () =>
    api.documentReview(reviewId)
      .then(setReview)
      .catch((e: any) => toast.error("Could not load this review", { description: e?.message ?? String(e) }));

  useEffect(() => { void load(); }, [reviewId]);

  if (!review) {
    return (
      <div className="rounded-xl border border-border bg-card p-6 text-sm text-muted-foreground flex items-center gap-2">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading…
      </div>
    );
  }

  const closed = review.state !== "open";
  const canResolve = review.can_resolve === true;
  const blocker = review.cannot_resolve_reason ?? null;

  const run = async (work: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    try {
      await work();
      toast.success(ok);
      await load();
      onDone();
    } catch (e: any) {
      toast.error(e?.message ?? String(e));
    } finally {
      setBusy(false);
    }
  };

  const comment = async () => {
    if (!draft.trim()) return;
    await run(async () => {
      await api.addReviewComment(review.id, { body: draft });
      setDraft("");
    }, "Comment added");
  };

  return (
    <div className="rounded-xl border border-border bg-card p-6 space-y-5">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-2 flex-wrap">
            <Badge variant="outline" className="text-[10px] text-purple border-purple/40 bg-purple/10">
              <MessageSquare className="h-3 w-3 mr-1" /> document review
            </Badge>
            <StatusChip status={review.document?.status} />
            {closed && (
              <span className="text-xs text-muted-foreground">
                {review.state} by {review.resolved_by_name ?? "someone"}
              </span>
            )}
          </div>
          <h2 className="text-lg font-semibold">{review.title || "Changes requested"}</h2>
          <p className="text-sm text-muted-foreground mt-1">{review.reason}</p>
          <div className="text-xs text-muted-foreground mt-1">
            Raised by {review.requested_by_name ?? "someone"}
            {review.assigned_to_name ? ` · assigned to ${review.assigned_to_name}` : ""}
          </div>
        </div>

        {review.document?.project_id && review.document?.id && (
          <Button asChild variant="outline" size="sm">
            <Link
              to="/projects/$id/edit/$docId"
              params={{ id: review.document.project_id, docId: review.document.id }}
            >
              <Pencil className="h-4 w-4 mr-1.5" /> Open and fix
            </Link>
          </Button>
        )}
      </div>

      {review.resolution_note && (
        <div className="rounded-lg border border-border bg-background/40 p-3 text-sm">
          <div className="text-xs uppercase tracking-wider text-muted-foreground mb-1">Outcome</div>
          {review.resolution_note}
        </div>
      )}

      <Conversation
        comments={review.comments ?? []}
        currentUserId={currentUserId}
        onResolve={(commentId) => run(() => api.resolveReviewComment(review.id, commentId), "Marked done")}
      />

      {!closed && (
        <>
          <Textarea
            rows={3} value={draft} onChange={(e) => setDraft(e.target.value)}
            placeholder="Add to the discussion…"
          />
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" onClick={comment} disabled={busy || !draft.trim()}>
              <MessageSquare className="h-4 w-4 mr-1.5" /> Comment
            </Button>
            <div className="ml-auto flex items-center gap-2">
              {review.requested_by === currentUserId && (
                <Button variant="ghost" size="sm" disabled={busy}
                        onClick={() => run(() => api.withdrawDocumentReview(review.id), "Withdrawn")}>
                  Withdraw
                </Button>
              )}
              {capabilities.includes("review_document") && (
                <>
                  <Button
                    variant="outline" size="sm" disabled={busy || !canResolve}
                    title={blocker ?? undefined}
                    onClick={() => { setNote(""); setRejectOpen(true); }}
                  >
                    <X className="h-4 w-4 mr-1.5" /> Changes still needed
                  </Button>
                  <Button
                    size="sm" disabled={busy || !canResolve} title={blocker ?? undefined}
                    onClick={() => run(() => api.approveDocumentReview(review.id),
                                       "Closed — no change needed")}
                  >
                    <CheckCircle2 className="h-4 w-4 mr-1.5" /> Looks right
                  </Button>
                </>
              )}
            </div>
          </div>
          {/* The server's own answer to "may I close this", so the four-eyes rule
              is learned before typing rather than as a 403 afterwards. */}
          {blocker && <p className="text-xs text-muted-foreground">{blocker}</p>}
        </>
      )}

      {review.document_version_id && <DocumentText versionId={review.document_version_id} />}

      <ReasonDialog
        open={rejectOpen} onOpenChange={setRejectOpen}
        title="What still needs to change?"
        description="It is what whoever fixes this will read."
        placeholder="e.g. use the 2026 band and re-check the pro-rata"
        confirmLabel="Confirm changes needed"
        destructive
        value={note} onValueChange={setNote}
        onConfirm={() => void run(async () => {
          await api.rejectDocumentReview(review.id, { note });
          setRejectOpen(false);
        }, "Recorded")}
      />
    </div>
  );
}

function Conversation({ comments, currentUserId, onResolve }: {
  comments: ReviewComment[];
  currentUserId: string;
  onResolve: (commentId: string) => void;
}) {
  if (comments.length === 0) return null;
  return (
    <div className="space-y-2">
      {comments.map((c) => (
        <div
          key={c.id}
          className={cn(
            "rounded-lg border px-3 py-2",
            c.resolved_at ? "border-border bg-muted/40 opacity-70" : "border-border bg-background/40",
          )}
        >
          <div className="flex items-center gap-2 text-xs text-muted-foreground mb-0.5">
            <span>{c.author_id === currentUserId ? "You" : c.author_name ?? "Someone"}</span>
            {c.resolved_at && <span className="text-success">· done</span>}
            {!c.resolved_at && (
              <button
                onClick={() => onResolve(c.id)}
                className="ml-auto hover:text-foreground underline decoration-dotted"
              >
                mark done
              </button>
            )}
          </div>
          {c.quoted_text && (
            // What the run said when the remark was written. "This figure is
            // wrong" is worthless once somebody has changed the figure.
            <blockquote className="border-l-2 border-purple/40 pl-2 mb-1 text-xs italic text-muted-foreground">
              {c.quoted_text}
              {c.paragraph_index != null && <span className="not-italic"> · ¶{c.paragraph_index}</span>}
            </blockquote>
          )}
          <div className="text-sm whitespace-pre-wrap">{c.body}</div>
        </div>
      ))}
    </div>
  );
}

/** The letter itself, read-only.
 *
 *  `GET /document-versions/{id}/text` already returns exactly the paragraph and
 *  span coordinates comments anchor on, so showing the document a reviewer is
 *  being asked about costs no backend work at all. */
function DocumentText({ versionId }: { versionId: string }) {
  const [paragraphs, setParagraphs] = useState<{ paragraph_index: number; text: string }[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open || paragraphs || error) return;
    api.documentText(versionId)
      .then((d) => setParagraphs(d.paragraphs.map((p) => ({ paragraph_index: p.paragraph_index, text: p.text }))))
      .catch((e: any) => setError(e?.message ?? String(e)));
  }, [open, versionId]);

  return (
    <div className="rounded-lg border border-border overflow-hidden">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-3 py-2 text-xs uppercase tracking-wider text-muted-foreground hover:bg-accent/40"
      >
        <FileText className="h-3.5 w-3.5" />
        {open ? "Hide the document" : "Read the document"}
      </button>
      {open && (
        <div className="max-h-80 overflow-auto border-t border-border p-4 space-y-2 text-sm">
          {error && <div className="text-destructive">{error}</div>}
          {!error && !paragraphs && (
            <div className="text-muted-foreground flex items-center gap-2">
              <Loader2 className="h-4 w-4 animate-spin" /> Reading…
            </div>
          )}
          {paragraphs?.filter((p) => p.text.trim()).map((p) => (
            <p key={p.paragraph_index} className="leading-relaxed">
              <span className="mr-2 text-[10px] font-mono text-muted-foreground align-super">
                {p.paragraph_index}
              </span>
              {p.text}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

/* --------------------------------------------------- the engine could not decide */

function TaskDetailLoader({ taskId, onDone }: { taskId: string; onDone: () => void }) {
  const [task, setTask] = useState<ReviewTask | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    api.reviewTask(taskId)
      .then((t) => { setTask(t); setError(null); })
      .catch((e: any) => setError(e?.message ?? String(e)));

  useEffect(() => { void load(); }, [taskId]);

  if (error) {
    return (
      <div className="rounded-xl border border-border bg-card p-6 text-sm space-y-2">
        <p className="text-destructive">{error}</p>
        <Button variant="outline" size="sm" onClick={() => void load()}>Try again</Button>
      </div>
    );
  }
  if (!task) {
    return (
      <div className="rounded-xl border border-border bg-card p-6 text-sm text-muted-foreground flex items-center gap-2">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading…
      </div>
    );
  }
  // `onDone` refreshes the queue; `load` refreshes this pane. Both are needed:
  // under the `all` filter the settled task is still in the list, so its id and
  // therefore this component's key never change and the mount effect does not
  // re-run -- the pane kept showing an editable form over a resolved task, and
  // pressing Resolve again produced "already resolved" with no sign the first
  // press had worked. `ReviewDetail` always did `await load(); onDone();`, which
  // is what made this an oversight rather than a decision.
  return <TaskDetail task={task} onDone={async () => { await load(); onDone(); }} />;
}

function TaskDetail({ task, onDone }: { task: ReviewTask; onDone: () => void | Promise<void> }) {
  const [value, setValue] = useState(task.proposed_value ?? "");
  const [rationale, setRationale] = useState("");
  const [promote, setPromote] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dismissOpen, setDismissOpen] = useState(false);
  const [dismissReason, setDismissReason] = useState("");
  const readOnly = task.status !== "open";

  const resolve = async () => {
    if (!rationale.trim()) {
      toast.error("A rationale is required", { description: "It becomes part of the document's audit record." });
      return;
    }
    setBusy(true);
    try {
      const r = await api.resolveReviewTask(task.id, {
        resolved_value: value || undefined,
        rationale,
        promote_to_manifest: promote,
        promote_as: task.kind === "calculation" ? "formula" : "condition_expression",
      });
      toast.success("Resolved", {
        description: r.promoted?.applied
          ? "Promoted into the manifest — this won't be asked again."
          : r.promoted?.reason,
      });
      await onDone();
    } catch (e: any) {
      toast.error("Could not resolve", { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  };

  const context = task.context ?? {};

  return (
    <div className="rounded-xl border border-border bg-card p-6 space-y-5">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <Badge variant="outline" className={cn("text-[10px] mb-2", KIND_TONE[task.kind])}>{task.kind}</Badge>
          <h2 className="text-lg font-semibold">{task.question}</h2>
          <code className="text-xs font-mono text-muted-foreground">{task.unit_id}</code>
        </div>
        {!readOnly && (
          <Button variant="ghost" size="sm" className="text-muted-foreground"
                  onClick={() => { setDismissReason(""); setDismissOpen(true); }}>
            <X className="h-4 w-4 mr-1.5" /> Dismiss
          </Button>
        )}
      </div>

      {context.expression && (
        <div>
          <div className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">Expression</div>
          <code className="block rounded-lg border border-border bg-background/40 p-3 text-sm font-mono break-all">
            {context.expression}
          </code>
        </div>
      )}

      {(context.inputs || context.available) && (
        <div>
          <div className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">Values it had</div>
          <div className="rounded-lg border border-border divide-y divide-border text-sm">
            {Object.entries({ ...(context.available ?? {}), ...(context.inputs ?? {}) }).map(([k, v]) => (
              <div key={k} className="flex items-center justify-between gap-3 px-3 py-2">
                <code className="text-xs font-mono text-brand">{k}</code>
                <span className={cn("truncate", v == null && "text-destructive")}>{v == null ? "(missing)" : String(v)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {readOnly ? (
        <div className={cn(
          "rounded-lg border p-4 text-sm space-y-1",
          task.status === "resolved" ? "border-success/30 bg-success/10" : "border-border bg-muted/40",
        )}>
          <div>
            <span className="font-medium">
              {task.status === "resolved" ? "Resolved to:" : "Dismissed."}
            </span>{" "}
            {task.resolved_value || (task.status === "resolved" ? "—" : "")}
          </div>
          <div className="text-muted-foreground">{task.rationale}</div>
          {task.resolved_by_name && (
            // Who decided, not only what was decided. A resolution whose resolver
            // is invisible is half an audit record.
            <div className="text-xs text-muted-foreground">by {task.resolved_by_name}</div>
          )}
        </div>
      ) : (
        <>
          <div>
            <div className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">
              {task.kind === "calculation" ? "Value or corrected formula" : "Resolved value"}
            </div>
            <Input value={value} onChange={(e) => setValue(e.target.value)} className="font-mono text-sm" placeholder="e.g. 7.14" />
          </div>

          <div>
            <div className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">Rationale (required)</div>
            <Textarea
              rows={3}
              value={rationale}
              onChange={(e) => setRationale(e.target.value)}
              placeholder="Why this value is correct — this is recorded in the document's lineage."
            />
          </div>

          <div className="flex items-center justify-between rounded-lg border border-border p-3">
            <div>
              <div className="text-sm font-medium">Promote into the manifest</div>
              <div className="text-xs text-muted-foreground">
                Turn this decision into a rule so the same question isn't asked on every future document.
              </div>
            </div>
            <Switch checked={promote} onCheckedChange={setPromote} />
          </div>

          <div className="flex justify-end">
            <Button onClick={resolve} disabled={busy} className="bg-gradient-brand text-white">
              {busy ? <Loader2 className="h-4 w-4 mr-1.5 animate-spin" /> : <ClipboardCheck className="h-4 w-4 mr-1.5" />}
              Resolve
            </Button>
          </div>
        </>
      )}

      {task.document_version_id && <DocumentText versionId={task.document_version_id} />}

      {/* Dismissing is a decision, and a regulated document records why a
          question was set aside as surely as why it was answered. The server
          requires the rationale; this asks for it rather than 400-ing. */}
      <ReasonDialog
        open={dismissOpen} onOpenChange={setDismissOpen}
        title="Why does this not need answering?"
        description="It becomes part of the audit record, alongside the questions that were answered."
        placeholder="e.g. this clause is not used in the UK variant of the letter"
        confirmLabel="Dismiss"
        value={dismissReason} onValueChange={setDismissReason}
        onConfirm={() => {
          api.dismissReviewTask(task.id, { rationale: dismissReason })
            .then(async () => { setDismissOpen(false); toast("Dismissed"); await onDone(); })
            .catch((e: any) => toast.error("Could not dismiss", { description: e?.message ?? String(e) }));
        }}
      />
    </div>
  );
}
