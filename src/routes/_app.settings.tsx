import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { User, Building2, Bell, Key, Cpu, CreditCard, Shield, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/_app/settings")({
  head: () => ({ meta: [{ title: "Settings — DocuMind AI" }, { name: "description", content: "Workspace and account settings." }] }),
  component: SettingsPage,
});

const TABS = [
  { id: "profile", label: "Profile", icon: User },
  { id: "workspace", label: "Workspace", icon: Building2 },
  { id: "ai", label: "AI Models", icon: Cpu },
  { id: "notifications", label: "Notifications", icon: Bell },
  { id: "security", label: "Security", icon: Shield },
  { id: "api", label: "API keys", icon: Key },
  { id: "billing", label: "Billing", icon: CreditCard },
];

function SettingsPage() {
  const [tab, setTab] = useState("profile");
  return (
    <div className="p-6 lg:p-8 max-w-6xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground mt-1">Manage your account, workspace, and integrations.</p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[220px_1fr] gap-8">
        <nav className="space-y-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={cn(
                "w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors",
                tab === t.id ? "bg-accent text-accent-foreground font-medium" : "hover:bg-muted/50 text-muted-foreground",
              )}
            >
              <t.icon className="h-4 w-4" /> {t.label}
            </button>
          ))}
        </nav>

        <div className="space-y-6">
          {tab === "profile" && <ProfileTab />}
          {tab === "workspace" && <WorkspaceTab />}
          {tab === "ai" && <AITab />}
          {tab === "notifications" && <NotificationsTab />}
          {tab === "security" && <SecurityTab />}
          {tab === "api" && <ApiTab />}
          {tab === "billing" && <BillingTab />}
        </div>
      </div>
    </div>
  );
}

function Section({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-border bg-card p-6">
      <div className="mb-5">
        <h2 className="font-semibold">{title}</h2>
        {description && <p className="text-xs text-muted-foreground mt-1">{description}</p>}
      </div>
      {children}
    </div>
  );
}

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-3 border-t border-border first:border-t-0 first:pt-0 gap-4">
      <div className="min-w-0">
        <div className="text-sm font-medium">{label}</div>
        {hint && <div className="text-xs text-muted-foreground mt-0.5">{hint}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function ProfileTab() {
  const [me, setMe] = useState<any>(null);
  useEffect(() => {
    api.me().then(setMe).catch((e: any) => toast.error("Could not load your profile", { description: e?.message ?? String(e) }));
  }, []);
  const initials = (me?.full_name ?? "")
    .split(" ").map((n: string) => n[0]).slice(0, 2).join("") || "–";
  return (
    <>
      <Section title="Profile" description="Your personal information visible to teammates.">
        <div className="flex items-center gap-4 mb-6">
          <Avatar className="h-16 w-16">
            <AvatarFallback className="bg-gradient-to-br from-primary to-purple-500 text-white text-lg">{initials}</AvatarFallback>
          </Avatar>
          <div>
            <Button variant="outline" size="sm">Change photo</Button>
            <p className="text-xs text-muted-foreground mt-2">PNG or JPG, up to 2 MB.</p>
          </div>
        </div>
        <div className="grid sm:grid-cols-2 gap-4">
          <div><Label>Full name</Label><Input value={me?.full_name ?? ""} readOnly className="mt-1.5" /></div>
          <div><Label>Email</Label><Input value={me?.email ?? ""} readOnly className="mt-1.5" /></div>
          <div><Label>Job title</Label><Input value={me?.job_title ?? ""} readOnly className="mt-1.5" /></div>
          <div><Label>Time zone</Label><Input value={me?.timezone ?? ""} readOnly className="mt-1.5" /></div>
        </div>
        <p className="text-xs text-muted-foreground mt-6">
          Profile editing isn't wired up yet — these values come from your account.
        </p>
      </Section>
    </>
  );
}

function WorkspaceTab() {
  return (
    <Section title="Workspace" description="Shared workspace settings for your organization.">
      <div className="grid sm:grid-cols-2 gap-4 mb-6">
        <div><Label>Workspace name</Label><Input defaultValue="Acme Life Sciences" className="mt-1.5" /></div>
        <div><Label>Workspace ID</Label><Input defaultValue="ws_5f2a8b1c" className="mt-1.5 font-mono text-xs" readOnly /></div>
        <div><Label>Default region</Label><Input defaultValue="Europe" className="mt-1.5" /></div>
        <div><Label>Default language</Label><Input defaultValue="English" className="mt-1.5" /></div>
      </div>
      <Row label="Public workspace" hint="Allow anyone with the link to view approved documents."><Switch /></Row>
      <Row label="Require approval before export" hint="Drafts must be approved by an admin before download."><Switch defaultChecked /></Row>
    </Section>
  );
}

function AITab() {
  const models = [
    { name: "GPT-4 Turbo", vendor: "OpenAI", context: "128K", pref: true },
    { name: "Claude 3.5 Sonnet", vendor: "Anthropic", context: "200K", pref: false },
    { name: "Gemini 1.5 Pro", vendor: "Google", context: "1M", pref: false },
    { name: "Llama 3.1 70B", vendor: "Meta (self-host)", context: "128K", pref: false },
  ];
  return (
    <>
      <Section title="Default model" description="Used for drafting when a project doesn't specify one.">
        <div className="space-y-2">
          {models.map((m) => (
            <label key={m.name} className={cn("flex items-center gap-3 p-3 rounded-lg border cursor-pointer", m.pref ? "border-primary bg-primary/5" : "border-border hover:bg-muted/40")}>
              <div className={cn("h-4 w-4 rounded-full border-2 flex items-center justify-center", m.pref ? "border-primary" : "border-muted-foreground")}>
                {m.pref && <div className="h-2 w-2 rounded-full bg-primary" />}
              </div>
              <div className="flex-1">
                <div className="text-sm font-medium">{m.name}</div>
                <div className="text-xs text-muted-foreground">{m.vendor} · {m.context} context</div>
              </div>
              {m.pref && <span className="text-xs font-medium text-primary">Selected</span>}
            </label>
          ))}
        </div>
      </Section>
      <Section title="Generation defaults">
        <Row label="Temperature" hint="Lower = more deterministic. Recommended 0.3 for regulated documents."><span className="text-sm tabular-nums">0.3</span></Row>
        <Row label="Cite sources inline" hint="Automatically add citations to every generated claim."><Switch defaultChecked /></Row>
        <Row label="Redact PII on export" hint="Detect and mask names, emails, and IDs in exported files."><Switch defaultChecked /></Row>
      </Section>
    </>
  );
}

function NotificationsTab() {
  return (
    <Section title="Notifications" description="Choose what you get notified about.">
      <Row label="Draft ready" hint="A generation you triggered finished."><Switch defaultChecked /></Row>
      <Row label="Approval requested" hint="A teammate asked you to approve a draft."><Switch defaultChecked /></Row>
      <Row label="Mentions in comments"><Switch defaultChecked /></Row>
      <Row label="Weekly workspace digest" hint="Every Monday, 9:00 local time."><Switch /></Row>
      <Row label="Product updates from DocuMind"><Switch /></Row>
    </Section>
  );
}

function SecurityTab() {
  return (
    <>
      <Section title="Sign-in" description="Protect access to your account.">
        <Row label="Two-factor authentication" hint="Enabled via authenticator app.">
          <span className="inline-flex items-center gap-1 text-xs text-emerald-500 font-medium"><Check className="h-3 w-3" /> Enabled</span>
        </Row>
        <Row label="SSO (Okta)" hint="Enforced for @company.com emails."><Switch defaultChecked /></Row>
        <Row label="Session timeout" hint="Sign users out after inactivity."><span className="text-sm">4 hours</span></Row>
      </Section>
      <Section title="Active sessions">
        {[
          { device: "MacBook Pro · Safari", loc: "Berlin, DE", when: "Current session" },
          { device: "iPhone 15 · Mobile Safari", loc: "Berlin, DE", when: "2 hours ago" },
          { device: "Windows · Chrome", loc: "Amsterdam, NL", when: "Yesterday" },
        ].map((s) => (
          <div key={s.device} className="flex items-center justify-between py-3 border-t border-border first:border-t-0 first:pt-0">
            <div>
              <div className="text-sm font-medium">{s.device}</div>
              <div className="text-xs text-muted-foreground">{s.loc} · {s.when}</div>
            </div>
            <Button variant="ghost" size="sm" className="text-red-500 hover:text-red-500">Revoke</Button>
          </div>
        ))}
      </Section>
    </>
  );
}

function ApiTab() {
  const keys = [
    { name: "Production", key: "sk_live_••••••••4f2a", created: "May 12, 2026", lastUsed: "2 min ago" },
    { name: "Staging", key: "sk_test_••••••••9c81", created: "May 12, 2026", lastUsed: "3 hours ago" },
    { name: "CI pipeline", key: "sk_test_••••••••1e77", created: "Jun 04, 2026", lastUsed: "Yesterday" },
  ];
  return (
    <Section title="API keys" description="Keys let scripts and integrations call DocuMind on your behalf.">
      <div className="space-y-2">
        {keys.map((k) => (
          <div key={k.name} className="flex items-center gap-4 p-3 rounded-lg border border-border">
            <Key className="h-4 w-4 text-muted-foreground shrink-0" />
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium">{k.name}</div>
              <div className="text-xs text-muted-foreground font-mono">{k.key}</div>
            </div>
            <div className="text-xs text-muted-foreground text-right">
              <div>Created {k.created}</div>
              <div>Last used {k.lastUsed}</div>
            </div>
            <Button variant="ghost" size="sm" className="text-red-500 hover:text-red-500">Revoke</Button>
          </div>
        ))}
      </div>
      <div className="mt-4"><Button size="sm">Create new key</Button></div>
    </Section>
  );
}

function BillingTab() {
  return (
    <>
      <Section title="Current plan">
        <div className="flex items-center justify-between">
          <div>
            <div className="flex items-center gap-2">
              <span className="text-xl font-semibold">Enterprise</span>
              <span className="text-xs px-2 py-0.5 rounded-md bg-primary/10 text-primary font-medium">Active</span>
            </div>
            <p className="text-sm text-muted-foreground mt-1">Unlimited seats · SSO · SOC 2 · 7-year retention</p>
          </div>
          <Button variant="outline">Manage plan</Button>
        </div>
        <div className="grid grid-cols-3 gap-4 mt-6">
          <div className="rounded-lg bg-muted/40 p-4">
            <div className="text-xs text-muted-foreground">Seats used</div>
            <div className="text-lg font-semibold mt-1 tabular-nums">10 / ∞</div>
          </div>
          <div className="rounded-lg bg-muted/40 p-4">
            <div className="text-xs text-muted-foreground">Tokens this cycle</div>
            <div className="text-lg font-semibold mt-1 tabular-nums">14.2M</div>
          </div>
          <div className="rounded-lg bg-muted/40 p-4">
            <div className="text-xs text-muted-foreground">Next invoice</div>
            <div className="text-lg font-semibold mt-1">Aug 01, 2026</div>
          </div>
        </div>
      </Section>
      <Section title="Payment method">
        <div className="flex items-center justify-between p-3 rounded-lg border border-border">
          <div className="flex items-center gap-3">
            <div className="h-8 w-12 rounded bg-gradient-to-br from-slate-700 to-slate-900 flex items-center justify-center text-white text-xs font-bold">VISA</div>
            <div>
              <div className="text-sm font-medium">Visa •••• 4242</div>
              <div className="text-xs text-muted-foreground">Expires 08 / 2028</div>
            </div>
          </div>
          <Button variant="ghost" size="sm">Update</Button>
        </div>
      </Section>
    </>
  );
}
