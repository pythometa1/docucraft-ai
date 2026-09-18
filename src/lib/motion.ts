/**
 * The motion system. One file, so the whole product moves like one machine.
 *
 * Every duration, spring and stagger in the app resolves here. The rule this
 * exists to enforce: an interface where each screen invented its own timing ends
 * up with six different ideas about how fast a thing should move, and the
 * difference is legible to a user even when they cannot name it.
 *
 * The JSX primitives that consume these live in `components/motion.tsx`, which
 * re-exports everything below — so `@/components/motion` and `@/lib/motion` are
 * the same vocabulary and no existing import had to change.
 *
 * Two hard rules encoded here rather than left to discipline:
 *
 *  - **Transform and opacity only.** Nothing in this file animates width,
 *    height, top or left. Those are laid out and painted every frame; transform
 *    and opacity are composited, which is the difference between 60fps and a
 *    janky panel on a mid-range laptop.
 *  - **Reduced motion collapses everything.** `useReducedMotionFlag` is checked
 *    in code as well as in the stylesheet, because a JavaScript-driven transform
 *    does not stop when CSS transitions are disabled — and vestibular triggers
 *    are why the setting exists, not a taste in polish.
 */
import { useEffect, useState } from "react";
import type { Transition } from "framer-motion";

/* -------------------------------- durations -------------------------------- */

/** Seconds. Micro-interactions stay under a quarter second, reveals under half.
 *  Anything slower than `revealSlow` is a loop, not a transition, and belongs in
 *  the stylesheet where it can be paused. */
export const DUR = {
  instant: 0.08,
  micro: 0.15,
  press: 0.18,
  base: 0.22,
  reveal: 0.32,
  revealSlow: 0.45,
  route: 0.2,
} as const;

/** Milliseconds. Hard caps for any sequence driven off a real request. */
export const CAP = {
  /** Total budget for a staged reveal. Nothing narrates for longer than this. */
  sequenceMs: 2500,
  /** Minimum time a stage stays legible while the request is still in flight. */
  stageMs: 320,
  /** Speed the remainder flushes at once the real work has landed. */
  flushMs: 120,
  /** Per-item stagger. */
  stepMs: 30,
  /** Items past this index arrive together. A batch here is one document per
   *  source row, so a 30ms step over a thousand rows would take half a minute to
   *  finish arriving — at which point the stagger is not polish, it is a delay. */
  staggerItems: 12,
} as const;

export const EASE_OUT = [0.16, 1, 0.3, 1] as const;
export const EASE_IN_OUT = [0.65, 0, 0.35, 1] as const;

/* --------------------------------- springs --------------------------------- */

/** Quick, no visible overshoot. A spring that bounces reads as a toy in a
 *  product about regulated documents. */
export const SPRING_UI: Transition = { type: "spring", stiffness: 420, damping: 34, mass: 0.7 };
/** Softer, for panels and reveals. */
export const SPRING_PANEL: Transition = { type: "spring", stiffness: 260, damping: 28, mass: 0.9 };
/** Progress and counters: settles, never overshoots. A bar that flies past the
 *  real figure and comes back has, for a moment, stated something untrue. */
export const SPRING_PROGRESS: Transition = { type: "spring", stiffness: 180, damping: 30, mass: 1 };
/** Dialog and palette entrance. */
export const SPRING_POP: Transition = { type: "spring", stiffness: 480, damping: 32, mass: 0.6 };

/* -------------------------------- staggering ------------------------------- */

/** Delay in seconds for item `index`, capped. Past the cap, zero. */
export function staggerDelay(index: number, stepMs: number = CAP.stepMs) {
  return index < CAP.staggerItems ? (index * stepMs) / 1000 : 0;
}

/* ------------------------------ reduced motion ----------------------------- */

/** Live `prefers-reduced-motion`, updating when the OS setting changes. */
export function useReducedMotionFlag() {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(mq.matches);
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return reduced;
}

/** True while the tab is in front. Ambient loops pause on false: a compositor
 *  animation nobody can see is battery spend with no upside, and this is an
 *  application people leave open all day. */
export function usePageVisible() {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const onChange = () => setVisible(!document.hidden);
    onChange();
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return visible;
}

/* --------------------------------- counters -------------------------------- */

/**
 * Counts up to a real number on an ease-out that settles rather than
 * overshooting. The target always comes from real data; this animates only the
 * approach to it.
 *
 * `enabled` exists for the case that matters most in this product: a metric with
 * no measurement must not animate to 0, because a counter ticking up to zero
 * reads as "we measured, and it was none" when the truth is "nobody measured".
 * Pass `enabled={value != null}` and render the reason instead.
 */
export function useCountUp(target: number, durationMs = 700, enabled = true) {
  const reduced = useReducedMotionFlag();
  const [value, setValue] = useState(reduced || !enabled ? target : 0);

  useEffect(() => {
    if (reduced || !enabled) {
      setValue(target);
      return;
    }
    let raf = 0;
    const start = performance.now();
    const tick = (t: number) => {
      const p = Math.min(1, (t - start) / durationMs);
      setValue(target * (1 - Math.pow(1 - p, 3)));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, durationMs, reduced, enabled]);

  return reduced || !enabled ? target : value;
}

/* ----------------------------- staged sequences ---------------------------- */

/**
 * Drives a staged reveal off a real request lifecycle.
 *
 * The rule that makes it honest: while `done` is false the sequence advances but
 * halts one short of complete, so the animation can never claim the work
 * finished before it did. When `done` flips, the remainder flushes at
 * `CAP.flushMs`. The whole thing is bounded by `CAP.sequenceMs`, so a slow
 * request does not leave a half-drawn list looking hung.
 */
export function useStagedReveal(stageCount: number, done: boolean) {
  const reduced = useReducedMotionFlag();
  const [index, setIndex] = useState(reduced ? stageCount : 0);

  useEffect(() => {
    if (reduced) {
      setIndex(done ? stageCount : Math.max(1, stageCount - 1));
      return;
    }
    const startedAt = performance.now();
    let timer: ReturnType<typeof setTimeout>;

    const step = () => {
      setIndex((i) => {
        const overBudget = performance.now() - startedAt >= CAP.sequenceMs;
        if (done || overBudget) {
          if (i >= stageCount) return stageCount;
          timer = setTimeout(step, CAP.flushMs);
          return i + 1;
        }
        timer = setTimeout(step, CAP.stageMs);
        return i >= stageCount - 1 ? i : i + 1;
      });
    };
    timer = setTimeout(step, CAP.stageMs);
    return () => clearTimeout(timer);
  }, [stageCount, done, reduced]);

  return {
    completed: Math.min(index, stageCount),
    activeIndex: index >= stageCount ? -1 : index,
    finished: index >= stageCount,
  };
}

/** Seconds elapsed since `running` went true. Deliberately not an estimate of
 *  time remaining: the work is a variable number of model rounds, so a countdown
 *  would be a figure the engine cannot honour, and a bar that stalls at 90% is
 *  worse than an honest clock. */
export function useElapsed(running: boolean) {
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    if (!running) {
      setSecs(0);
      return;
    }
    const startedAt = Date.now();
    const id = setInterval(() => setSecs(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    return () => clearInterval(id);
  }, [running]);
  return secs;
}
