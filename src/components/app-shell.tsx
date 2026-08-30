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
  Bell,
  Sparkles,
  HelpCircle,
  PanelLeftClose,
  PanelLeft,
  Menu,
  X,
  Sun,
  Moon,
  LogOut,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { useStore } from "@/lib/store";
import { useTheme } from "@/lib/theme";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";

const NAV = [
  { to: "/dashboard", label: "Projects", icon: FolderKanban },
  { to: "/chat", label: "Chat", icon: MessageSquare },
  { to: "/templates", label: "Templates", icon: FileText },
  { to: "/review", label: "Review", icon: ClipboardCheck },
  { to: "/analytics", label: "Analytics", icon: BarChart3 },
  // §22's numbers, which were measured continuously and shown nowhere. A
  // role without READ_AUDIT gets a plain explanation rather than a 403 toast,
  // so this is not hidden from the nav.
  { to: "/quality", label: "Quality", icon: Gauge },
  { to: "/team", label: "Team", icon: Users },
  { to: "/audit-log", label: "Audit Log", icon: Shield },
  { to: "/settings", label: "Settings", icon: Settings },
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
  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  return (
    <div className="flex min-h-screen bg-background text-foreground">
      {/* Desktop sidebar */}
      <aside
        className={cn(
          "hidden md:flex flex-col border-r border-border bg-sidebar transition-[width] duration-200 ease-in-out",
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

      {/* Main */}
      <div className="flex-1 flex flex-col min-w-0">
        <header className="h-14 border-b border-border bg-background/70 backdrop-blur-md sticky top-0 z-30 flex items-center gap-3 px-4 md:px-6">
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

          <div className="flex-1 max-w-xl">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <input
                className="w-full h-9 rounded-lg bg-surface border border-border pl-9 pr-16 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring/50"
                placeholder="Search projects, documents, templates…"
              />
              <kbd className="absolute right-2 top-1/2 -translate-y-1/2 text-[10px] font-mono text-muted-foreground bg-muted px-1.5 py-0.5 rounded border border-border">
                ⌘K
              </kbd>
            </div>
          </div>
          <button
            onClick={toggle}
            className="p-2 rounded-lg hover:bg-accent text-muted-foreground hover:text-foreground"
            aria-label="Toggle theme"
            title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          >
            {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </button>
          <button className="p-2 rounded-lg hover:bg-accent text-muted-foreground hover:text-foreground">
            <HelpCircle className="h-4 w-4" />
          </button>
          <button className="p-2 rounded-lg hover:bg-accent text-muted-foreground hover:text-foreground relative">
            <Bell className="h-4 w-4" />
            <span className="absolute top-1.5 right-1.5 h-1.5 w-1.5 rounded-full bg-destructive" />
          </button>
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
        <main className="flex-1 min-w-0">{children}</main>
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
                "flex items-center rounded-lg text-sm transition-colors",
                collapsed ? "justify-center h-10 w-10 mx-auto" : "gap-3 px-3 py-2",
                active
                  ? "bg-sidebar-accent text-foreground"
                  : "text-muted-foreground hover:bg-sidebar-accent hover:text-foreground",
              )}
            >
              <Icon className="h-4 w-4 shrink-0" />
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
