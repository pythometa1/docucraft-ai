import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { MoreHorizontal, Mail, Copy, Shield } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

export const Route = createFileRoute("/_app/team")({
  head: () => ({ meta: [{ title: "Team — DocuMind AI" }, { name: "description", content: "Team and permission management." }] }),
  component: TeamPage,
});

type Member = { name: string; email: string; role: "Owner" | "Admin" | "Editor" | "Viewer"; function: string; status: "Active" | "Invited" | "Inactive"; lastActive: string; docs: number };



const ROLE_DESCRIPTIONS: Record<string, string> = {
  org_admin: "Full workspace control, billing, and destructive actions.",
  admin: "Manage members, templates, and settings within their function.",
  editor: "Create and edit projects, drafts, and templates.",
  viewer: "Read-only access to approved documents.",
};

const roleTone: Record<string, string> = {
  Owner: "bg-purple-500/10 text-purple-500 border-purple-500/20",
  Admin: "bg-blue-500/10 text-blue-500 border-blue-500/20",
  Editor: "bg-emerald-500/10 text-emerald-500 border-emerald-500/20",
  Viewer: "bg-muted text-muted-foreground border-border",
};

const statusTone: Record<string, string> = {
  Active: "bg-emerald-500/10 text-emerald-500",
  Invited: "bg-amber-500/10 text-amber-500",
  Inactive: "bg-muted text-muted-foreground",
};

function initials(name: string) {
  return name.split(" ").map((n) => n[0]).slice(0, 2).join("");
}

function TeamPage() {
  const [members, setMembers] = useState<Member[]>([]);
  const [roles, setRoles] = useState<{ role: string; count: number }[]>([]);
  const [query, setQuery] = useState("");
  const [copying, setCopying] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.teamMembers(), api.teamRolesSummary()])
      .then(([m, r]) => {
        setMembers(m.items.map((x: any) => ({
          name: x.name, email: x.email,
          role: (x.role ?? "viewer").replace("org_admin", "Owner").replace(/^./, (c: string) => c.toUpperCase()) as Member["role"],
          function: x.function ?? "—",
          status: (x.status ?? "active").replace(/^./, (c: string) => c.toUpperCase()) as Member["status"],
          lastActive: x.last_active ? new Date(x.last_active).toLocaleString() : "—",
          docs: x.docs ?? 0,
        })));
        setRoles(r.items);
      })
      .catch((e: any) => toast.error("Could not load the team", { description: e?.message ?? String(e) }));
  }, []);

  async function copyEmail(email: string) {
    setCopying(email);
    try {
      await navigator.clipboard.writeText(email);
      toast.success("Email address copied");
    } catch (e: any) {
      // Clipboard access is refused outside a secure context or without
      // permission, so the address has to be shown for manual copying.
      toast.error("Could not copy the email address", { description: e?.message ?? email });
    } finally {
      setCopying(null);
    }
  }

  const q = query.trim().toLowerCase();
  const MEMBERS = q
    ? members.filter((m) =>
        [m.name, m.email, m.function, m.role].some((v) => v.toLowerCase().includes(q)),
      )
    : members;
  const ROLES = roles.map((r) => ({
    role: r.role.replace("org_admin", "Owner").replace(/^./, (c) => c.toUpperCase()),
    desc: ROLE_DESCRIPTIONS[r.role] ?? "Workspace member.",
    count: r.count,
  }));

  return (
    <div className="p-6 lg:p-8 max-w-7xl mx-auto space-y-6">
      <div className="flex items-start justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Team</h1>
          <p className="text-sm text-muted-foreground mt-1">{members.length} member{members.length === 1 ? "" : "s"}.</p>
        </div>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {ROLES.map((r) => (
          <div key={r.role} className="rounded-xl border border-border bg-card p-5">
            <div className="flex items-center gap-2">
              <Shield className="h-4 w-4 text-muted-foreground" />
              <span className="text-sm font-semibold">{r.role}</span>
            </div>
            <div className="text-2xl font-semibold mt-2 tabular-nums">{r.count}</div>
            <p className="text-xs text-muted-foreground mt-1.5 line-clamp-2">{r.desc}</p>
          </div>
        ))}
      </div>

      <div className="rounded-xl border border-border bg-card">
        <div className="p-4 border-b border-border flex items-center gap-3">
          <Input
            placeholder="Search members by name, email, function…"
            className="max-w-sm"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <div className="ml-auto text-xs text-muted-foreground">
            {MEMBERS.length} {MEMBERS.length === 1 ? "member" : "members"}
            {q && members.length !== MEMBERS.length ? ` of ${members.length}` : ""}
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-muted/40">
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="px-4 py-3 font-medium">Member</th>
                <th className="px-4 py-3 font-medium whitespace-nowrap">Role</th>
                <th className="px-4 py-3 font-medium whitespace-nowrap">Function</th>
                <th className="px-4 py-3 font-medium whitespace-nowrap">Status</th>
                <th className="px-4 py-3 font-medium whitespace-nowrap">Docs</th>
                <th className="px-4 py-3 font-medium whitespace-nowrap">Last active</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {MEMBERS.map((m) => (
                <tr key={m.email} className="hover:bg-muted/30 transition-colors">
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-3 min-w-0">
                      <Avatar className="h-8 w-8">
                        <AvatarFallback className="text-xs bg-gradient-to-br from-primary to-purple-500 text-white">
                          {initials(m.name)}
                        </AvatarFallback>
                      </Avatar>
                      <div className="min-w-0">
                        <div className="font-medium truncate">{m.name}</div>
                        <div className="text-xs text-muted-foreground truncate">{m.email}</div>
                      </div>
                    </div>
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium border ${roleTone[m.role] ?? roleTone.Viewer}`}>{m.role}</span>
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap"><Badge variant="secondary">{m.function}</Badge></td>
                  <td className="px-4 py-3 whitespace-nowrap">
                    <span className={`inline-flex items-center gap-1.5 text-xs px-2 py-0.5 rounded-md ${statusTone[m.status] ?? statusTone.Inactive}`}>
                      <span className="h-1.5 w-1.5 rounded-full bg-current" /> {m.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 tabular-nums whitespace-nowrap">{m.docs}</td>
                  <td className="px-4 py-3 whitespace-nowrap text-muted-foreground">{m.lastActive}</td>
                  <td className="px-4 py-3 text-right">
                    <DropdownMenu>
                      <DropdownMenuTrigger className="p-1.5 rounded-md hover:bg-muted data-[state=open]:bg-muted">
                        <MoreHorizontal className="h-4 w-4" />
                        <span className="sr-only">Actions for {m.name}</span>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end" className="w-52">
                        <DropdownMenuItem
                          disabled={copying === m.email}
                          onSelect={() => void copyEmail(m.email)}
                        >
                          <Copy className="h-4 w-4 mr-2" /> Copy email address
                        </DropdownMenuItem>
                        <DropdownMenuItem asChild>
                          <a href={`mailto:${m.email}`}>
                            <Mail className="h-4 w-4 mr-2" /> Send email
                          </a>
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </td>
                </tr>
              ))}
              {MEMBERS.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-4 py-10 text-center text-sm text-muted-foreground">
                    {members.length === 0 ? "No team members yet." : `No members match “${query.trim()}”.`}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
