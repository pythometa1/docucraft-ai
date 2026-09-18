import { createFileRoute, Link } from "@tanstack/react-router";
import {
  Sparkles,
  ArrowRight,
  ArrowDown,
  ShieldCheck,
  Layers,
  MessageSquare,
  Workflow,
  CheckCircle2,
  FileCheck,
  Users2,
  FlaskConical,
  ClipboardCheck,
  Stethoscope,
  Megaphone,
  AlertTriangle,
  Scale,
  Upload,
  Wand2,
  FileText,
} from "lucide-react";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "DocuMind AI — Turn manual document work into AI workflows" },
      {
        name: "description",
        content:
          "Generate HR letters, clinical reports, and regulatory documents in seconds. Map templates to your data with precision and full audit control.",
      },
      { property: "og:title", content: "DocuMind AI" },
      { property: "og:description", content: "AI document generation for enterprise teams." },
    ],
  }),
  component: Landing,
});

function Landing() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* Nav */}
      <header className="sticky top-0 z-30 border-b border-border bg-background/70 backdrop-blur-md">
        <div className="mx-auto max-w-7xl px-6 h-16 flex items-center justify-between">
          <Link to="/" className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-brand">
              <Sparkles className="h-4 w-4 text-white" />
            </div>
            <span className="font-semibold">DocuMind AI</span>
          </Link>
          <nav className="hidden md:flex items-center gap-8 text-sm text-muted-foreground">
            <a href="#features" className="hover:text-foreground">Features</a>
            <a href="#usecases" className="hover:text-foreground">Use cases</a>
            <a href="#how" className="hover:text-foreground">How it works</a>
            <Link to="/docs" className="hover:text-foreground">Docs</Link>
          </nav>
          <div className="flex items-center gap-2">
            <Link to="/dashboard" className="text-sm text-muted-foreground hover:text-foreground px-3 py-1.5">
              Sign in
            </Link>
            <Link
              to="/dashboard"
              className="text-sm bg-gradient-brand text-white rounded-lg px-4 py-2 font-medium hover:opacity-90"
            >
              Get started
            </Link>
          </div>
        </div>
      </header>

      {/* Hero */}
      <section className="relative overflow-hidden bg-hero-orbs">
        <div className="mx-auto max-w-7xl px-6 py-24 md:py-32 grid md:grid-cols-2 gap-12 items-center">
          <div>
            <div className="inline-flex items-center gap-2 rounded-full border border-border bg-surface px-3 py-1 text-xs text-muted-foreground mb-6">
              <Sparkles className="h-3 w-3 text-brand" />
              AI-native document platform for regulated industries
            </div>
            <h1 className="text-5xl md:text-6xl font-bold leading-[1.05] tracking-tight">
              Turn manual document work into{" "}
              <span className="text-gradient">AI-powered workflows</span>
            </h1>
            <p className="mt-6 text-lg text-muted-foreground max-w-xl">
              Generate HR letters, clinical reports, and regulatory documents in seconds. Map templates to
              your data sources with precision and full audit control.
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <Link
                to="/dashboard"
                className="inline-flex items-center gap-2 bg-gradient-brand text-white rounded-lg px-5 py-3 font-medium hover:opacity-90"
              >
                Get started free <ArrowRight className="h-4 w-4" />
              </Link>
              <a
                href="#how"
                className="inline-flex items-center gap-2 border border-border bg-surface rounded-lg px-5 py-3 font-medium hover:bg-accent"
              >
                <ArrowDown className="h-4 w-4" /> See how it works
              </a>
            </div>
            <div className="mt-10 flex flex-wrap items-center gap-x-6 gap-y-2 text-xs text-muted-foreground">
              {["SOC 2 Type II", "HIPAA Compliant", "GDPR Ready", "ISO 27001"].map((t) => (
                <div key={t} className="inline-flex items-center gap-1.5">
                  <ShieldCheck className="h-3.5 w-3.5 text-success" /> {t}
                </div>
              ))}
            </div>
          </div>
          <HeroIllustration />
        </div>
      </section>

      {/* Features */}
      <section id="features" className="py-24 border-t border-border">
        <div className="mx-auto max-w-7xl px-6">
          <div className="max-w-2xl mb-14">
            <h2 className="text-3xl md:text-4xl font-bold tracking-tight">
              Enterprise document generation, without the overhead
            </h2>
            <p className="mt-4 text-muted-foreground">
              Every feature designed for regulated environments — with speed and rigor built in.
            </p>
          </div>
          <div className="grid md:grid-cols-3 gap-4">
            {FEATURES.map((f) => (
              <div key={f.title} className="rounded-xl surface-raised p-6 hover:border-border-strong transition-colors">
                <div className="h-10 w-10 rounded-lg bg-brand/10 border border-brand/20 flex items-center justify-center mb-4">
                  <f.icon className="h-5 w-5 text-brand" />
                </div>
                <h3 className="font-semibold mb-2">{f.title}</h3>
                <p className="text-sm text-muted-foreground">{f.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Use cases */}
      <section id="usecases" className="py-24 border-t border-border bg-surface/30">
        <div className="mx-auto max-w-7xl px-6">
          <h2 className="text-3xl md:text-4xl font-bold tracking-tight mb-14">Built for every team</h2>
          <div className="grid md:grid-cols-3 gap-4">
            {USE_CASES.map((u) => (
              <div key={u.title} className="rounded-xl surface-raised p-6">
                <u.icon className="h-6 w-6 text-purple mb-4" />
                <h3 className="font-semibold mb-2">{u.title}</h3>
                <p className="text-sm text-muted-foreground">{u.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* How it works */}
      <section id="how" className="py-24 border-t border-border">
        <div className="mx-auto max-w-7xl px-6">
          <h2 className="text-3xl md:text-4xl font-bold tracking-tight mb-14">How it works</h2>
          <div className="grid md:grid-cols-4 gap-4">
            {STEPS.map((s, i) => (
              <div key={s.title} className="relative rounded-xl surface-raised p-6">
                <div className="text-xs text-muted-foreground font-mono mb-3">Step {i + 1}</div>
                <s.icon className="h-6 w-6 text-brand mb-3" />
                <h3 className="font-semibold mb-2">{s.title}</h3>
                <p className="text-sm text-muted-foreground">{s.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Final CTA */}
      <section className="py-24 border-t border-border bg-hero-orbs">
        <div className="mx-auto max-w-4xl px-6 text-center">
          <h2 className="text-4xl md:text-5xl font-bold tracking-tight">
            Ready to <span className="text-gradient">generate documents</span> at enterprise scale?
          </h2>
          <p className="mt-4 text-muted-foreground max-w-xl mx-auto">
            Join teams shipping regulated documents in hours instead of weeks.
          </p>
          <Link
            to="/dashboard"
            className="mt-8 inline-flex items-center gap-2 bg-gradient-brand text-white rounded-lg px-6 py-3 font-medium hover:opacity-90"
          >
            Get started free <ArrowRight className="h-4 w-4" />
          </Link>
        </div>
      </section>

      <footer className="border-t border-border py-10">
        <div className="mx-auto max-w-7xl px-6 flex flex-wrap items-center justify-between gap-4 text-xs text-muted-foreground">
          <div>© 2026 DocuMind AI. All rights reserved.</div>
          <div className="flex gap-6">
            <a href="#features" className="hover:text-foreground">Features</a>
            <a href="#usecases" className="hover:text-foreground">Use cases</a>
            <a href="#how" className="hover:text-foreground">How it works</a>
            <Link to="/docs" className="hover:text-foreground">Documentation</Link>
            <Link to="/login" search={{ redirect: undefined }} className="hover:text-foreground">Sign in</Link>
          </div>
        </div>
      </footer>
    </div>
  );
}

const FEATURES = [
  { icon: Layers, title: "Smart Template Mapping", desc: "Map sections of your Word/HTML templates to columns in your source data with AI precision." },
  { icon: Workflow, title: "Multi-Source Fusion", desc: "Combine data from CSVs, JSONs, PDFs, and databases into one document." },
  { icon: MessageSquare, title: "Conversational Generation", desc: "Chat with AI to describe what you need — get a document back." },
  { icon: CheckCircle2, title: "Section-Level Control", desc: "Choose exactly which sections get AI-generated, which stay verbatim." },
  { icon: Users2, title: "Approval Workflows", desc: "Multi-step review with role-based approvals for regulated industries." },
  { icon: FileCheck, title: "Complete Audit Trail", desc: "Every change tracked. Every prompt logged. Regulatory-ready." },
];

const USE_CASES = [
  { icon: Users2, title: "Human Resources", desc: "Offer letters, termination letters, promotion memos, policy communications." },
  { icon: FlaskConical, title: "Clinical Research", desc: "Study reports, protocol amendments, informed consent forms." },
  { icon: ClipboardCheck, title: "Quality / CMC", desc: "Batch records, deviation reports, CMC sections for regulatory filings." },
  { icon: Stethoscope, title: "Medical Affairs", desc: "Medical letters, publication summaries, standard response documents." },
  { icon: Megaphone, title: "Marketing", desc: "Product briefs, campaign copy, localized content." },
  { icon: AlertTriangle, title: "Safety & Legal", desc: "Adverse event reports, PSURs, safety communications." },
];

const STEPS = [
  { icon: Upload, title: "Upload your template", desc: "Word, HTML, or plain text — we detect placeholders automatically." },
  { icon: FileText, title: "Import source data", desc: "CSV, Excel, JSON, or PDF — combine multiple files." },
  { icon: Wand2, title: "Map sections with AI", desc: "Auto-map or fine-tune per section with our mapping wizard." },
  { icon: Scale, title: "Generate & approve", desc: "Review, edit, and route through approvals with full audit trail." },
];

function HeroIllustration() {
  return (
    <div className="relative aspect-square max-w-md ml-auto">
      <div className="absolute inset-0 rounded-3xl bg-gradient-to-br from-brand/20 via-purple/15 to-transparent blur-2xl" />
      <div className="relative rounded-2xl surface-raised/70 backdrop-blur-md p-6 shadow-2xl">
        <div className="flex items-center gap-2 mb-4">
          <div className="h-2 w-2 rounded-full bg-destructive/70" />
          <div className="h-2 w-2 rounded-full bg-warning/70" />
          <div className="h-2 w-2 rounded-full bg-success/70" />
        </div>
        <div className="space-y-3">
          <div className="h-3 rounded bg-muted w-3/4" />
          <div className="h-3 rounded bg-muted w-full" />
          <div className="h-3 rounded bg-muted w-5/6" />
          <div className="my-4 h-px bg-border" />
          <div className="flex items-center gap-2">
            <div className="h-8 w-8 rounded-lg bg-gradient-brand flex items-center justify-center">
              <Sparkles className="h-4 w-4 text-white" />
            </div>
            <div className="flex-1">
              <div className="h-2.5 rounded bg-muted w-1/2 mb-1.5" />
              <div className="h-2 rounded bg-muted w-1/3" />
            </div>
          </div>
          <div className="rounded-lg border border-brand/30 bg-brand/5 p-3">
            <div className="h-2 rounded bg-brand/40 w-2/3 mb-2" />
            <div className="h-2 rounded bg-muted w-full mb-1" />
            <div className="h-2 rounded bg-muted w-5/6" />
          </div>
        </div>
      </div>
    </div>
  );
}
