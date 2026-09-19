/**
 * What this organisation *requires* of the model provider, kept visible.
 *
 * §16 records two things per tenant: a residency zone, and whether zero
 * retention is contractually required. Both were enforced server-side and shown
 * nowhere, so a customer who had negotiated EU-only processing had no way to
 * see that we knew about it.
 *
 * The framing is the whole design, so it is worth stating plainly:
 *
 * **This chip reports a requirement, not a confirmation.** `/admin/data-policy`
 * answers with what the organisation has told us; the provider's actual
 * deployment configuration and its real retention behaviour are not exposed
 * over HTTP to this frontend at all. A green tick reading "zero retention" here
 * would be the single most damaging thing on the screen -- a security claim
 * with nothing behind it -- so every string says "required", the panel says
 * whose statement it is out loud, and the palette is deliberately neutral
 * rather than `ai-confident`.
 *
 * **No model names.** The panel used to list the models billed recently. Which
 * vendors and models run the product is ours to know, not a customer's, so the
 * panel is about the customer's data -- where it may be processed and how long
 * it is kept -- and nothing else.
 *
 * **There is no in-flight pulse.** Nothing reports AI work in flight for the
 * workspace, and a pulse driven off a guess would be an animation asserting
 * something nobody knows, so the chip does not pulse.
 */

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Shield } from "lucide-react";

import { api, type DataPolicy } from "@/lib/api";
import { useStore } from "@/lib/store";
import { DUR, EASE_OUT, SPRING_PANEL, useReducedMotionFlag } from "@/components/motion";
import { cn } from "@/lib/utils";

/** The capability `/admin/data-policy` is gated on, named as `authz` names it.
 *  Checked before the request rather than after a 403: a role that cannot read
 *  this should not be spending a round trip discovering so on every page. */
const POLICY_CAPABILITY = "manage_users";

/** `GLOBAL` is the platform's "no zone stated", not a zone. */
const NO_ZONE = "GLOBAL";

export function ModelBoundaryChip() {
  const capabilities = useStore((s) => s.capabilities);
  const canRead = capabilities.includes(POLICY_CAPABILITY);

  const [policy, setPolicy] = useState<DataPolicy | null>(null);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotionFlag();

  useEffect(() => {
    if (!canRead) return;
    let live = true;
    api.dataPolicy()
      .then((p) => { if (live) setPolicy(p); })
      // Swallowed on purpose. This is chrome in a header, not content: a role
      // change, a revoked capability or a flaky request must leave the header
      // looking like a header, never carrying an error about a panel nobody
      // opened.
      .catch(() => { /* not visible to this session */ });
    return () => { live = false; };
  }, [canRead]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Nothing to say, so nothing on the screen. The alternative -- a chip reading
  // "policy not visible to your role" -- is noise in a header for every role
  // that cannot act on it.
  if (!policy) return null;

  const zone = policy.residency && policy.residency !== NO_ZONE ? policy.residency : null;
  const parts = [
    zone ? `${zone} required` : null,
    policy.zero_retention_required ? "zero-retention required" : null,
  ].filter(Boolean) as string[];
  const summary = parts.length ? parts.join(" · ") : "No boundary required";

  return (
    <div ref={rootRef} className="relative">
      <motion.button
        initial={reduced ? false : { opacity: 0, y: -4 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: DUR.base, ease: EASE_OUT }}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="dialog"
        title="Data residency & retention"
        className={cn(
          // Neutral, never `ai-confident`: a green pill in a header is read as
          // "verified", and nothing here has been verified.
          "inline-flex h-8 max-w-[15rem] items-center gap-1.5 rounded-lg border px-2.5",
          "text-[11px] font-medium transition-colors duration-150",
          open
            ? "border-border-strong bg-accent text-foreground"
            : "border-border bg-surface/60 text-muted-foreground hover:bg-accent/60 hover:text-foreground",
        )}
      >
        {/* `Shield`, never `ShieldCheck`. A shield with a tick beside "EU
            required · zero-retention required" reads as *attested*, which is the
            one assertion every string in this component is written to avoid --
            a glyph makes the claim the copy refuses to. Plain shield: a boundary
            has been asked for. Nothing here says it was checked. */}
        <Shield className="h-3.5 w-3.5 shrink-0" />
        <span className="truncate">{summary}</span>
      </motion.button>

      <AnimatePresence>
        {open && (
          <motion.div
            role="dialog"
            aria-label="Data residency & retention"
            initial={reduced ? { opacity: 0 } : { opacity: 0, y: -6, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={reduced ? { opacity: 0 } : { opacity: 0, y: -4, scale: 0.98 }}
            transition={reduced ? { duration: DUR.instant } : SPRING_PANEL}
            style={{ transformOrigin: "top right" }}
            // `surface-glass` is for floating overlays, which is exactly what
            // this is -- it never wraps page content.
            className="absolute right-0 top-[calc(100%+8px)] z-50 w-[19rem] rounded-xl surface-glass p-3.5 shadow-xl"
          >
            <p className="text-[13px] font-semibold tracking-tight">Data residency &amp; retention</p>
            <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
              Where this workspace&rsquo;s data may be processed, and how long it is kept.
            </p>

            <dl className="mt-3 space-y-2">
              <Row
                term="Region required"
                value={zone ?? "None recorded"}
                muted={!zone}
              />
              <Row
                term="Zero retention"
                value={policy.zero_retention_required ? "Required" : "Not required"}
                muted={!policy.zero_retention_required}
              />
              <Row
                term="Source files kept"
                value={`${policy.source_retention_days} days`}
              />
              <Row
                term="Documents kept"
                value={policy.generated_documents_retained_indefinitely
                  ? "No period set"
                  : `${policy.generated_document_retention_days} days`}
                muted={policy.generated_documents_retained_indefinitely}
              />
            </dl>

            {!policy.recorded && (
              <p className="mt-2.5 rounded-lg border border-ai-uncertain/35 bg-ai-uncertain/10 px-2.5 py-1.5 text-[11px] leading-relaxed text-ai-uncertain">
                No policy has been recorded for this workspace. The retention above is the
                platform default standing in for one.
              </p>
            )}

            {/* The sentence this whole component exists to be honest about. */}
            <p className="mt-3 border-t border-border/70 pt-2.5 text-[11px] leading-relaxed text-muted-foreground">
              These are your organisation&rsquo;s recorded requirements, not a report of what
              any processor retains.
            </p>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function Row({ term, value, muted }: { term: string; value: string; muted?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-[11px] text-muted-foreground">{term}</dt>
      <dd className={cn("text-[11px] font-medium tabular-nums",
                        muted ? "text-muted-foreground" : "text-foreground")}>
        {value}
      </dd>
    </div>
  );
}
