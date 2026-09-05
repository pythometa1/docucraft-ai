import { toast } from "sonner";
import { Link, useLocation } from "@tanstack/react-router";
import { useEffect, useState, type ReactNode } from "react";
import {
  FolderKanban,
  MessageSquare,
  FileText,
  BarChart3,
  Gauge,
  Users,
  Shield,
  Settings,
  ClipboardCheck,
  Search,
  ReceiptText,
  Sparkles,
  HelpCircle,
  PanelLeftClose,
  PanelLeft,
  Menu,
  X,
  Sun,
  Moon,
  LogOut,
  BookOpen,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { useStore } from "@/lib/store";
import { useTheme } from "@/lib/theme";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { CommandPalette } from "@/components/command-palette";
import { ModelBoundaryChip } from "@/components/model-boundary-chip";

const NAV = [
  { to: "/dashboard", label: "Projects", icon: FolderKanban },
  { to: "/chat", label: "Chat", icon: MessageSquare },
  { to: "/templates", label: "Templates", icon: FileText },
  { to: "/invoices", label: "Invoices", icon: ReceiptText },
  { to: "/review", label: "Review", icon: ClipboardCheck },
  { to: "/analytics", label: "Analytics", icon: BarChart3 },
  // §22's numbers, which were measured continuously and shown nowhere. A
  // role without READ_AUDIT gets a plain explanation rather than a 403 toast,
  // so this is not hidden from the nav.
  { to: "/quality", label: "Quality", icon: Gauge },
  { to: "/team", label: "Team", icon: Users },
  { to: "/audit-log", label: "Audit Log", icon: Shield },
  { to: "/settings", label: "Settings", icon: Settings },
  { to: "/docs", label: "Docs", icon: BookOpen },
];

function initials(name: string): string {
  return name.split(" ").filter(Boolean).map((n) => n[0]).join("").slice(0, 2).toUpperCase();
}

export function AppShell({ children }: { children: ReactNode }) {
  const location = useLocation();
  const user = useStore((s) => s.currentUser);
  const loadCurrentUser = useStore((s) => s.loadCurrentUser);
  const { theme, toggle } = useTheme();

  // Who is actually signed in, asked of the server. The name in the header used
  // to be a constant in the bundle, so it read the same no matter whose token
  // the app was holding.
  useEffect(() => {
    // A failure here is usually an expired or rejected token, which is exactly
    // the case the header must not hide: swallowed, the app renders a signed-in
    // shell around a session the server has already stopped honouring, and every
    // screen inside it fails separately with no explanation.
    void loadCurrentUser().catch((e: any) =>
      toast.error("Could not confirm who is signed in", { description: e?.message ?? String(e) }),
    );
  }, [loadCurrentUser]);

  async function signOut() {
    await api.logout();
    window.location.assign("/login");
  }

  // Desktop: collapsed rail vs expanded. Persist preference.
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem("dm.sidebar.collapsed") === "1";
  });
  useEffect(() => {
    window.localStorage.setItem("dm.sidebar.collapsed", collapsed ? "1" : "0");
  }, [collapsed]);

  // Mobile: pop-up drawer
  const [mobileOpen, setMobileOpen] = useState(false);

  // The command palette the ⌘K hint has always promised.
  const [paletteOpen, setPaletteOpen] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  return (
    // Translucent rather than solid: `Atmosphere` is a fixed layer at -z-10, and
    // an opaque background here would paint straight over it. At 88% the aurora
    // reads as a tint in the corners and nothing else -- body text keeps its full
    // contrast against a near-solid ground.
    <div className="flex min-h-screen bg-background/88 text-foreground">
      {/* Desktop sidebar */}
      <aside
        className={cn(
          "hidden md:flex flex-col border-r border-border bg-sidebar/85 backdrop-blur-xl transition-[width] duration-200 ease-in-out",
          collapsed ? "w-16" : "w-60",
        )}
      >
        <SidebarInner
          collapsed={collapsed}
          user={user}
          pathname={location.pathname}
          onToggle={() => setCollapsed((c) => !c)}
        />
      </aside>

      {/* Mobile drawer + backdrop */}
      <div
        className={cn(
          "md:hidden fixed inset-0 z-50 transition-opacity",
          mobileOpen ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none",
        )}
      >
        <div
          className="absolute inset-0 bg-black/60 backdrop-blur-sm"
          onClick={() => setMobileOpen(false)}
        />
        <aside
          className={cn(
            "absolute left-0 top-0 h-full w-64 bg-sidebar border-r border-border flex flex-col shadow-2xl transition-transform duration-200 ease-out",
            mobileOpen ? "translate-x-0" : "-translate-x-full",
          )}
        >
          <SidebarInner
            collapsed={false}
            user={user}
            pathname={location.pathname}
            onToggle={() => setMobileOpen(false)}
            toggleIcon={<X className="h-4 w-4" />}
          />
        </aside>
      </div>

      <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} />

      {/* Main */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-border bg-background/70 px-4 backdrop-blur-md backdrop-saturate-150 md:px-6">
          {/* Mobile menu button */}
          <button
            className="md:hidden p-2 rounded-lg hover:bg-accent text-muted-foreground hover:text-foreground"
            onClick={() => setMobileOpen(true)}
            aria-label="Open menu"
          >
            <Menu className="h-4 w-4" />
          </button>
          {/* Desktop show-when-collapsed toggle (extra affordance) */}
          {collapsed && (
            <button
              className="hidden md:inline-flex p-2 rounded-lg hover:bg-accent text-muted-foreground hover:text-foreground"
              onClick={() => setCollapsed(false)}
              aria-label="Expand sidebar"
              title="Expand sidebar"
            >
              <PanelLeft className="h-4 w-4" />
            </button>
          )}

          {/* This was an input with no handler and a ⌘K hint that did nothing --
              a search box that could be typed into and never searched anything.
              `cmdk` and `ui/command.tsx` were already installed, so it is a real
              palette now. A button rather than an input, because it opens
              something rather than accepting text in place, and a box that looks
              like a field but rejects the cursor is worse than a button. */}
          <div className="max-w-xl flex-1">
            <button
              onClick={() => setPaletteOpen(true)}
              className="group flex h-9 w-full items-center gap-2 rounded-lg surface-raised px-3 text-left text-sm text-muted-foreground transition-colors hover:border-border-strong hover:bg-accent/40"
            >
              <Search className="h-4 w-4 shrink-0" />
              <span className="truncate">Search projects, documents, templates…</span>
              <kbd className="ml-auto hidden shrink-0 rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] sm:inline">
                ⌘K
              </kbd>
            </button>
          </div>
          {/* §16's residency and zero-retention requirements, which were
              enforced server-side and visible nowhere. It renders itself only
              for a session that may read them, so most roles see the header
              exactly as before. */}
          <div className="hidden sm:block">
            <ModelBoundaryChip />
          </div>
          <button
            onClick={toggle}
            className="p-2 rounded-lg hover:bg-accent text-muted-foreground hover:text-foreground"
            aria-label="Toggle theme"
            title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          >
            {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </button>
          {/* Was a button with no handler; the destination it implied already
              exists in the nav, so it goes there. */}
          <Link
            to="/docs"
            className="p-2 rounded-lg hover:bg-accent text-muted-foreground hover:text-foreground"
            aria-label="Documentation"
            title="Documentation"
          >
            <HelpCircle className="h-4 w-4" />
          </Link>
          {/* The bell is gone rather than silenced. It had no handler and wore a
              permanent unread dot -- a badge asserting there is something to
              read, drawn over a feature that does not exist. Nothing in the API
              reports notifications, so there is nothing here to show. */}
          <button
            onClick={signOut}
            className="p-2 rounded-lg hover:bg-accent text-muted-foreground hover:text-foreground"
            aria-label="Sign out"
            title="Sign out"
          >
            <LogOut className="h-4 w-4" />
          </button>
          <Avatar className="h-8 w-8">
            <AvatarFallback className="bg-gradient-brand text-white text-xs">
              {initials(user)}
            </AvatarFallback>
          </Avatar>
        </header>
        {/* The grid sits behind the page, at an alpha where it is not a pattern
            anybody looks at -- it is what stops a large empty area reading as a
            rendering failure. */}
        <main className="relative min-w-0 flex-1">
          <div aria-hidden className="pointer-events-none absolute inset-0 grid-noise opacity-[0.35]" />
          <div className="relative">{children}</div>
        </main>
        <footer className="border-t border-border py-3 px-6 text-xs text-muted-foreground text-center">
          © 2026 DocuMind AI | All rights reserved.
        </footer>
      </div>
    </div>
  );
}

function SidebarInner({
  collapsed,
  user,
  pathname,
  onToggle,
  toggleIcon,
}: {
  collapsed: boolean;
  user: string;
  pathname: string;
  onToggle: () => void;
  toggleIcon?: ReactNode;
}) {
  return (
    <>
      <div
        className={cn(
          "flex h-14 items-center border-b border-border",
          collapsed ? "justify-center px-2" : "gap-2 px-5",
        )}
      >
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-brand">
          <Sparkles className="h-4 w-4 text-white" />
        </div>
        {!collapsed && (
          <>
            <span className="font-semibold tracking-tight">DocuMind AI</span>
            <button
              onClick={onToggle}
              className="ml-auto p-1.5 rounded-md hover:bg-accent text-muted-foreground hover:text-foreground"
              aria-label="Collapse sidebar"
              title="Collapse sidebar"
            >
              {toggleIcon ?? <PanelLeftClose className="h-4 w-4" />}
            </button>
          </>
        )}
      </div>

      {collapsed && (
        <button
          onClick={onToggle}
          className="mx-2 mt-2 p-2 rounded-md hover:bg-accent text-muted-foreground hover:text-foreground flex items-center justify-center"
          aria-label="Expand sidebar"
          title="Expand sidebar"
        >
          <PanelLeft className="h-4 w-4" />
        </button>
      )}

      <nav className={cn("flex-1 space-y-1", collapsed ? "p-2" : "p-3")}>
        {NAV.map((item) => {
          const active = pathname.startsWith(item.to);
          const Icon = item.icon;
          return (
            <Link
              key={item.to}
              to={item.to}
              title={collapsed ? item.label : undefined}
              className={cn(
                "group relative flex items-center rounded-lg text-sm transition-all duration-200",
                collapsed ? "mx-auto h-10 w-10 justify-center" : "gap-3 px-3 py-2",
                active
                  ? "bg-sidebar-accent text-foreground shadow-[inset_0_1px_0_oklch(1_0_0/0.06)]"
                  : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground",
              )}
            >
              {/* The lit edge on the active item. An absolutely-positioned bar
                  rather than a border, so switching pages never shifts the row
                  by a pixel. */}
              {active && (
                <span
                  aria-hidden
                  className={cn(
                    "absolute rounded-full bg-gradient-brand",
                    collapsed
                      ? "inset-x-2 -bottom-0.5 h-0.5"
                      : "inset-y-1.5 left-0 w-0.5",
                  )}
                />
              )}
              <Icon className={cn("h-4 w-4 shrink-0 transition-colors", active && "text-brand")} />
              {!collapsed && <span>{item.label}</span>}
            </Link>
          );
        })}
      </nav>
      <div className="border-t border-border p-3">
        <div
          className={cn(
            "flex items-center rounded-lg p-1",
            collapsed ? "justify-center" : "gap-3 p-2",
          )}
        >
          <Avatar className="h-8 w-8 shrink-0">
            <AvatarFallback className="bg-gradient-brand text-white text-xs">
              {initials(user)}
            </AvatarFallback>
          </Avatar>
          {!collapsed && (
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium">{user || "…"}</div>
              <div className="text-xs text-muted-foreground">Enterprise</div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
