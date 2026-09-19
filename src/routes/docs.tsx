/**
 * The public overview: what the product does, at the level of outcomes.
 *
 * Deliberately short. This page ships to anyone who loads the site, so the full
 * authoring guide and API reference are served behind a login at /guide.
 */

import { createFileRoute, Link } from "@tanstack/react-router";
import { ArrowRight, BookOpen, FileCheck, Lock, ShieldCheck, Sparkles } from "lucide-react";

import { FadeIn } from "@/components/motion";

export const Route = createFileRoute("/docs")({
  head: () => ({
    meta: [
      { title: "Documentation — DocuMind AI" },
      { name: "description", content: "An overview of DocuMind AI: turn your own Word templates and data into finished, approved documents." },
    ],
  }),
  component: DocsPage,
});

const STEPS = [
  { title: "Bring your template", text: "Upload the Word document you already send. Your letterhead, tables and formatting stay exactly as they are." },
  { title: "Add your data", text: "Get a spreadsheet made for your template, fill in one row per document, and upload it." },
  { title: "Generate", text: "Your data is matched to the template for you. Every document is checked before it's ready." },
  { title: "Review and approve", text: "Send anything you disagree with to review, approve what's right, and download the finished documents." },
];

const PROMISES = [
  { icon: ShieldCheck, title: "Checked before anyone sees it", text: "Every document is checked against its template, and one that fails is held back rather than sent." },
  { icon: FileCheck, title: "Nothing leaves without approval", text: "Only an approved document can be downloaded, and every approval is recorded." },
  { icon: Lock, title: "Your data stays yours", text: "Every project is private to your organisation, and every action is kept in an audit trail." },
];

function DocsPage() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* The marketing header, not the application shell: this page is public. */}
      <header className="sticky top-0 z-30 border-b border-border bg-background/70 backdrop-blur-md backdrop-saturate-150">
        <div className="mx-auto flex h-16 max-w-[1100px] items-center justify-between px-6 lg:px-8">
          <Link to="/" className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-brand">
              <Sparkles className="h-4 w-4 text-white" />
            </div>
            <span className="font-semibold">DocuMind AI</span>
            <span className="ml-1 rounded-md border border-border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
              Docs
            </span>
          </Link>
          <div className="flex items-center gap-2">
            <Link to="/" className="px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground">
              Home
            </Link>
            <Link to="/guide"
                  className="rounded-lg bg-gradient-brand px-4 py-2 text-sm font-medium text-white hover:opacity-90">
              Sign in
            </Link>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1100px] space-y-12 px-6 py-8 pb-24 lg:px-8">
        <FadeIn className="relative overflow-hidden rounded-2xl surface-raised p-8 md:p-10">
          <div aria-hidden className="pointer-events-none absolute inset-0 bg-hero-orbs opacity-60" />
          <div className="relative">
            <div className="font-mono text-[11px] uppercase tracking-wider text-brand">Documentation</div>
            <h1 className="mt-2 text-4xl font-bold tracking-tight text-gradient md:text-5xl">
              Your templates, your data, finished documents
            </h1>
            <p className="mt-3 max-w-2xl text-[15px] leading-relaxed text-muted-foreground">
              DocuMind AI turns the documents you already send into a repeatable process. Give it
              your template and your data, and get back one finished, checked document per row,
              ready for approval.
            </p>
          </div>
        </FadeIn>

        <section>
          <h2 className="text-xl font-semibold tracking-tight">How it works</h2>
          <div className="mt-5 grid gap-4 sm:grid-cols-2">
            {STEPS.map((step, i) => (
              <div key={step.title} className="relative rounded-xl border border-border bg-background/40 p-4 pl-14">
                <div className="absolute left-4 top-4 flex h-7 w-7 items-center justify-center rounded-full bg-gradient-brand font-mono text-xs font-semibold text-white">
                  {i + 1}
                </div>
                <div className="text-[14px] font-medium">{step.title}</div>
                <p className="mt-1 text-[13.5px] leading-relaxed text-muted-foreground">{step.text}</p>
              </div>
            ))}
          </div>
        </section>

        <section>
          <h2 className="text-xl font-semibold tracking-tight">Security and approval</h2>
          <div className="mt-5 grid gap-4 md:grid-cols-3">
            {PROMISES.map((p) => (
              <div key={p.title} className="rounded-xl border border-border bg-background/40 p-4">
                <p.icon className="h-5 w-5 text-brand" />
                <div className="mt-2 text-[14px] font-medium">{p.title}</div>
                <p className="mt-1 text-[13.5px] leading-relaxed text-muted-foreground">{p.text}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-border bg-background/40 p-6">
          <div className="flex items-start gap-3">
            <BookOpen className="mt-0.5 h-5 w-5 shrink-0 text-brand" />
            <div>
              <div className="text-[15px] font-medium">The full guide</div>
              <p className="mt-1 text-[13.5px] text-muted-foreground">
                How to prepare a template, optional sections, document states and the API are
                covered in the guide for signed-in users.
              </p>
            </div>
          </div>
          <Link to="/guide"
                className="inline-flex items-center gap-2 rounded-lg bg-gradient-brand px-4 py-2 text-sm font-medium text-white hover:opacity-90">
            Sign in to read the full guide <ArrowRight className="h-4 w-4" />
          </Link>
        </section>
      </main>
    </div>
  );
}
