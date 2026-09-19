import { create } from "zustand";
import { toast } from "sonner";
import { api, ApiError } from "./api";
import { useStore } from "./store";
import type { ReadingFacts, ReadingResult, ReadingStep } from "./types";

/**
 * Template readings that run on the server while the person does something else.
 *
 * The polling lives here rather than in the dialog, because closing the dialog
 * ("Run in background") or changing route must not stop the progress arriving.
 * Tokens are kept in localStorage so a reload re-attaches; the templates list
 * re-attaches too (its `reading` field), which covers a reading started in
 * another tab or on another device.
 */

export type ReadingStatus = "running" | "done" | "failed";

export interface ReadingTask {
  token: string;
  templateName: string;
  projectId: string;
  templateId: string;
  /** Epoch ms. Corrected from the server's own clock on the first poll, so a
   *  re-attached task does not restart its timer at zero. */
  startedAt: number;
  finishedAt?: number;
  status: ReadingStatus;
  stages: ReadingStep[];
  facts: ReadingFacts;
  result: ReadingResult | null;
  error: string | null;
  /** Only known when this browser uploaded the file. */
  sizeBytes?: number;
}

type NewTask = Pick<ReadingTask, "token" | "templateName" | "projectId" | "templateId"> &
  Partial<Pick<ReadingTask, "startedAt" | "sizeBytes">>;

interface BackgroundTasks {
  tasks: Record<string, ReadingTask>;
  /** The task the dialog is showing. A task being watched finishes on screen,
   *  so it gets no toast as well. */
  watching: string | null;
  /** A template to bring into view on the project page ("View template"). */
  focus: { projectId: string; templateId: string } | null;
  track: (task: NewTask) => void;
  attachFromTemplates: (projectId: string, templates: any[]) => void;
  dismiss: (token: string) => void;
  setWatching: (token: string | null) => void;
  setFocus: (focus: { projectId: string; templateId: string } | null) => void;
}

const STORAGE_KEY = "dm.readings.v1";
const POLL_MS = 1200;
/** A finished task stays in the tray long enough to be noticed, then goes. */
const LINGER_MS = 8000;

type Stored = Pick<ReadingTask, "token" | "templateName" | "projectId" | "templateId" | "startedAt" | "sizeBytes">;

function loadStored(): Stored[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((t) => t && typeof t.token === "string") : [];
  } catch {
    return [];
  }
}

function saveStored(tasks: Record<string, ReadingTask>) {
  try {
    const running: Stored[] = Object.values(tasks)
      .filter((t) => t.status === "running")
      .map(({ token, templateName, projectId, templateId, startedAt, sizeBytes }) =>
        ({ token, templateName, projectId, templateId, startedAt, sizeBytes }));
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(running));
  } catch {
    // Private mode or storage full: the templates list still re-attaches.
  }
}

/** "23 values · 4 optional sections · 2 to check", from the real result only. */
export function summaryLine(result: ReadingResult | null | undefined): string {
  if (!result) return "";
  const parts = [
    `${result.field_count} value${result.field_count === 1 ? "" : "s"}`,
    `${result.condition_count} optional section${result.condition_count === 1 ? "" : "s"}`,
  ];
  if (result.warning_count) parts.push(`${result.warning_count} to check`);
  return parts.join(" · ");
}

/** Steps finished, out of three. The percentage is this and nothing smoother. */
export function completedSteps(task: Pick<ReadingTask, "status" | "stages">): number {
  if (task.status === "done") return 3;
  return task.stages.filter((s) => s.status === "done").length;
}

/** 0-based index of the step under way. */
export function currentStepIndex(task: Pick<ReadingTask, "status" | "stages">): number {
  if (!task.stages.length) return 0;
  const i = task.stages.findIndex((s) => s.status !== "done");
  return i === -1 ? task.stages.length - 1 : i;
}

// Navigation is the router's, and the store has no router. The shell registers
// one on mount; without it the toast's Open falls back to a full page load.
let navigate: ((projectId: string) => void) | null = null;
export function registerNavigator(fn: ((projectId: string) => void) | null) {
  navigate = fn;
}

let timer: ReturnType<typeof setInterval> | null = null;
let inFlight = false;

export const useBackgroundTasks = create<BackgroundTasks>((set, get) => ({
  tasks: {},
  watching: null,
  focus: null,

  track: (task) => {
    set((s) => ({
      tasks: {
        ...s.tasks,
        [task.token]: {
          ...s.tasks[task.token],
          ...task,
          startedAt: task.startedAt ?? s.tasks[task.token]?.startedAt ?? Date.now(),
          status: "running",
          stages: s.tasks[task.token]?.stages ?? [],
          facts: s.tasks[task.token]?.facts ?? {},
          result: null,
          error: null,
        },
      },
    }));
    saveStored(get().tasks);
    ensurePolling();
  },

  attachFromTemplates: (projectId, templates) => {
    const known = get().tasks;
    for (const t of templates ?? []) {
      const token = t?.reading?.progress_token;
      if (!token || known[token] || t.reading.status !== "running") continue;
      get().track({ token, templateName: t.name, projectId, templateId: t.id });
    }
  },

  dismiss: (token) => {
    set((s) => {
      const next = { ...s.tasks };
      delete next[token];
      return { tasks: next };
    });
    saveStored(get().tasks);
  },

  setWatching: (token) => set({ watching: token }),
  setFocus: (focus) => set({ focus }),
}));

function ensurePolling() {
  if (timer || typeof window === "undefined") return;
  void pollOnce();
  timer = setInterval(() => void pollOnce(), POLL_MS);
}

function stopIfIdle() {
  const running = Object.values(useBackgroundTasks.getState().tasks).some((t) => t.status === "running");
  if (!running && timer) {
    clearInterval(timer);
    timer = null;
  }
}

async function pollOnce() {
  if (inFlight) return;
  inFlight = true;
  try {
    const running = Object.values(useBackgroundTasks.getState().tasks).filter((t) => t.status === "running");
    await Promise.all(running.map(pollTask));
  } finally {
    inFlight = false;
    stopIfIdle();
  }
}

async function pollTask(task: ReadingTask) {
  let job;
  try {
    job = await api.getReadingJob(task.token);
  } catch (e) {
    // A 404 on a token nobody knows any more (another tenant, a wiped
    // database): stop asking. Anything else is transient; the next tick is a
    // better answer than an error.
    if (e instanceof ApiError && e.status === 404 && Date.now() - task.startedAt > 15000) {
      useBackgroundTasks.getState().dismiss(task.token);
    }
    return;
  }
  const status: ReadingStatus =
    job.status === "done" ? "done" : job.status === "failed" ? "failed" : "running";
  const serverStart =
    typeof job.elapsed_seconds === "number" ? Date.now() - job.elapsed_seconds * 1000 : null;
  const prev = useBackgroundTasks.getState().tasks[task.token];
  if (!prev) return;
  const next: ReadingTask = {
    ...prev,
    status,
    stages: job.stages ?? prev.stages,
    facts: job.facts ?? prev.facts,
    result: job.result ?? null,
    error: job.error ?? null,
    // Only ever moved earlier: the local clock started at the click, the
    // server's at the row, and the earlier of the two is the honest one.
    startedAt: serverStart != null ? Math.min(prev.startedAt, serverStart) : prev.startedAt,
    finishedAt: status === "running" ? undefined : Date.now(),
  };
  useBackgroundTasks.setState((s) => ({ tasks: { ...s.tasks, [task.token]: next } }));
  if (status !== "running") settle(next);
}

function settle(task: ReadingTask) {
  const state = useBackgroundTasks.getState();
  saveStored(state.tasks);

  // The project on screen reflects the reading as soon as it lands.
  if (typeof window !== "undefined" && window.location.pathname.startsWith(`/projects/${task.projectId}`)) {
    void useStore.getState().loadProjectDetail(task.projectId).catch(() => undefined);
  }

  if (state.watching !== task.token) {
    const open = () => {
      useBackgroundTasks.getState().setFocus({ projectId: task.projectId, templateId: task.templateId });
      if (navigate) navigate(task.projectId);
      else window.location.assign(`/projects/${task.projectId}`);
    };
    if (task.status === "done") {
      toast.success(`Template ready — ${task.templateName}`, {
        description: summaryLine(task.result) || undefined,
        action: { label: "Open", onClick: open },
      });
    } else {
      toast.error(`Couldn't read ${task.templateName}`, {
        description: "The file was uploaded. Use “Read again” on its row to try once more.",
        action: { label: "Open", onClick: open },
      });
    }
  }

  // Out of the tray after a moment, unless the dialog is still showing it.
  setTimeout(() => {
    const s = useBackgroundTasks.getState();
    if (s.watching !== task.token && s.tasks[task.token]?.status !== "running") s.dismiss(task.token);
  }, LINGER_MS);
}

// Re-attach whatever this browser was watching before the reload.
if (typeof window !== "undefined") {
  const stored = loadStored();
  if (stored.length) {
    useBackgroundTasks.setState({
      tasks: Object.fromEntries(stored.map((t) => [t.token, {
        ...t,
        startedAt: t.startedAt ?? Date.now(),
        status: "running" as const, stages: [], facts: {}, result: null, error: null,
      }])),
    });
    ensurePolling();
  }
}
