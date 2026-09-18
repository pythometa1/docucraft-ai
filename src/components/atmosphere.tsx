import { usePageVisible, useReducedMotionFlag } from "@/components/motion";

/**
 * The ambient ground the whole app sits on: three slowly drifting blurred orbs
 * and a fine grain over the top.
 *
 * It is fixed and behind everything, so it never re-animates on navigation --
 * the page content transitions, the room it is in does not.
 *
 * Two things switch it off. `prefers-reduced-motion`, because a permanently
 * moving background is exactly what that setting is for; and the tab being
 * hidden, because a compositor animation nobody is looking at is a battery cost
 * with no upside, and this is an application people leave open all day.
 */
export function Atmosphere() {
  const visible = usePageVisible();
  const reduced = useReducedMotionFlag();
  const state = visible && !reduced ? "running" : "paused";

  return (
    <div aria-hidden className="pointer-events-none fixed inset-0 -z-10 overflow-hidden">
      <div className="aurora-orb aurora-orb-1" style={{ animationPlayState: state }} />
      <div className="aurora-orb aurora-orb-2" style={{ animationPlayState: state }} />
      <div className="aurora-orb aurora-orb-3" style={{ animationPlayState: state }} />
      <div className="grain-overlay" />
    </div>
  );
}
