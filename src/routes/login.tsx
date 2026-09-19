import { useState } from "react";
import { createFileRoute, useNavigate, useSearch } from "@tanstack/react-router";
import { Sparkles, Loader2 } from "lucide-react";

import { Atmosphere } from "@/components/atmosphere";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export const Route = createFileRoute("/login")({
  validateSearch: (search: Record<string, unknown>) => ({
    redirect: typeof search.redirect === "string" ? search.redirect : undefined,
  }),
  head: () => ({ meta: [{ title: "Sign in — DocuMind AI" }] }),
  component: Login,
});

function Login() {
  const navigate = useNavigate();
  const { redirect } = useSearch({ from: "/login" });
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.login(email.trim(), password);
      // Never honour a redirect back to the sign-in screen: that turns a
      // successful login into an apparent failure.
      navigate({ to: redirect && !redirect.startsWith("/login") ? redirect : "/dashboard" });
    } catch (err) {
      // The backend deliberately does not say which half was wrong.
      setError(err instanceof ApiError ? err.message : "Could not reach the server.");
      setBusy(false);
    }
  }

  return (
    <>
      {/* The ambient layer runs here too. Sign-in is the first thing anyone
          sees, and a flat ground is a poor first impression of a product whose
          whole pitch is that the details were looked after. */}
      <Atmosphere />
      <div className="min-h-screen grid place-items-center bg-background/85 px-4">
      <div className="w-full max-w-sm">
        <div className="flex items-center gap-2 mb-8 justify-center">
          <div className="h-9 w-9 rounded-lg bg-gradient-brand grid place-items-center">
            <Sparkles className="h-5 w-5 text-white" />
          </div>
          <span className="text-lg font-semibold">DocuMind AI</span>
        </div>

        <form onSubmit={onSubmit} className="rounded-xl surface-raised p-6 space-y-4">
          <div>
            <h1 className="text-xl font-semibold">Sign in</h1>
            <p className="text-sm text-muted-foreground mt-1">Use the account created during setup.</p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>

          {error && (
            <div role="alert" className="text-sm rounded-lg border border-destructive/40 bg-destructive/10 text-destructive px-3 py-2">
              {error}
            </div>
          )}

          <Button type="submit" className="w-full" disabled={busy || !email || !password}>
            {busy && <Loader2 className="h-4 w-4 animate-spin" />}
            {busy ? "Signing in…" : "Sign in"}
          </Button>

          <p className="text-xs text-muted-foreground">
            No account yet? Ask your administrator for an account.
          </p>
        </form>
      </div>
      </div>
    </>
  );
}
