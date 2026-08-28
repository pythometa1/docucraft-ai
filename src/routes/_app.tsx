import { useEffect, useRef, useState } from "react";
import { createFileRoute, Outlet, useNavigate, useRouterState } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import { api } from "@/lib/api";

export const Route = createFileRoute("/_app")({
  component: RequireAuth,
});

/**
 * Every page under /_app needs a session. The check runs on the client rather
 * than in `beforeLoad` because the token lives in localStorage, which the SSR
 * pass cannot see -- guarding on the server would bounce every first paint to
 * /login even for a signed-in user.
 */
function RequireAuth() {
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const [authed, setAuthed] = useState<boolean | null>(null);
  // This component stays mounted for one more render while the router unwinds
  // to /login, and `pathname` has already flipped by then -- without the latch
  // the effect re-fires and rewrites the redirect target to /login itself, so
  // signing in successfully lands you straight back on the sign-in screen.
  const redirected = useRef(false);

  useEffect(() => {
    if (api.isAuthenticated()) {
      setAuthed(true);
      return;
    }
    setAuthed(false);
    if (redirected.current) return;
    redirected.current = true;
    navigate({ to: "/login", search: { redirect: pathname }, replace: true });
  }, [navigate, pathname]);

  if (!authed) {
    return <div className="min-h-screen grid place-items-center text-sm text-muted-foreground">Checking your session…</div>;
  }

  return (
    <AppShell>
      <Outlet />
    </AppShell>
  );
}
