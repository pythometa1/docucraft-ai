/**
 * The product's own documentation, served from the product.
 *
 * Two audiences in one page, and the split is deliberate. Somebody who has just
 * been given a login needs the five steps and the rules for marking up a Word
 * file; somebody wiring this into their HR system needs the endpoints and a
 * working sequence of calls. Splitting them into two sites means one of them
 * goes stale, so they sit under one contents list and share the same examples.
 *
 * Every endpoint named here was read off the running server's OpenAPI document
 * rather than written from memory.
 *
 * Deliberately outside `_app`, so it is reachable without a login. An API
 * reference somebody must be given an account to read is one they cannot use to
 * decide whether to integrate at all, and this page carries no customer data --
 * only the shape of the endpoints.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import {
  BookOpen, Check, ChevronRight, Copy, FileText, Braces, Workflow as WorkflowIcon,
  Terminal, Shield, Boxes, Search, Sparkles,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { FadeIn } from "@/components/motion";

export const Route = createFileRoute("/docs")({
  head: () => ({
    meta: [
      { title: "Documentation — DocuMind AI" },
      { name: "description", content: "How to use DocuMind AI, how to mark up a template, and the HTTP API for integrating it with your own systems." },
    ],
  }),
  component: DocsPage,
});

/* ------------------------------------------------------------------ data */

type Method = "GET" | "POST" | "PATCH" | "DELETE";

const METHOD_TONE: Record<Method, string> = {
  GET: "bg-info/12 text-info border-info/30",
  POST: "bg-success/12 text-success border-success/30",
  PATCH: "bg-warning/12 text-warning border-warning/30",
  DELETE: "bg-destructive/12 text-destructive border-destructive/30",
};

type Endpoint = { method: Method; path: string; what: string; note?: string };

const API: { id: string; title: string; blurb: string; endpoints: Endpoint[] }[] = [
  {
    id: "api-auth",
    title: "Authentication",
    blurb:
      "Every call except the health checks carries a bearer token. Tokens expire, so mint a "
      + "fresh one rather than caching one for the life of a long job.",
    endpoints: [
      { method: "POST", path: "/auth/token", what: "Exchange an email and password for an access token." },
      { method: "GET", path: "/me", what: "The signed-in user, their organisation and their capabilities." },
      { method: "POST", path: "/auth/logout", what: "Revoke the current token immediately." },
    ],
  },
  {
    id: "api-projects",
    title: "Projects",
    blurb: "A project holds the templates, the source data and the documents produced from them.",
    endpoints: [
      { method: "GET", path: "/projects", what: "List projects. Supports q, status, limit and offset." },
      { method: "POST", path: "/projects", what: "Create one. Requires name, region, function and document_type." },
      { method: "GET", path: "/projects/{id}", what: "One project." },
      { method: "PATCH", path: "/projects/{id}", what: "Rename, re-describe, or set the locale used for dates and numbers." },
      { method: "DELETE", path: "/projects/{id}", what: "Remove a project and everything inside it." },
      { method: "GET", path: "/lookups?kind=region", what: "Valid values for region, function and document_type." },
    ],
  },
  {
    id: "api-templates",
    title: "Templates",
    blurb:
      "Uploading a template reads it in the same act: the response tells you how many values "
      + "and conditions were found, and names any placeholder nothing can fill.",
    endpoints: [
      { method: "POST", path: "/projects/{id}/templates", what: "Upload a .docx. Multipart, field name file." },
      { method: "GET", path: "/projects/{id}/templates", what: "List them, with what was read from each." },
      { method: "POST", path: "/templates/{id}/compile-manifest", what: "Read the template again — after you have edited it." },
      { method: "GET", path: "/templates/{id}/manifests", what: "Every reading of this template, newest first." },
      { method: "DELETE", path: "/templates/{id}", what: "Remove one." },
    ],
  },
  {
    id: "api-data",
    title: "Source data",
    blurb:
      "Ask the template for the spreadsheet it wants, rather than guessing at column names. "
      + "It comes back with one column per value and a fixed list of choices on every column "
      + "that decides which sections are kept.",
    endpoints: [
      { method: "GET", path: "/template-manifests/{id}/source-template", what: "Download the spreadsheet this template asks for (.xlsx)." },
      { method: "POST", path: "/projects/{id}/sources", what: "Upload the filled spreadsheet. Multipart, field name file." },
      { method: "GET", path: "/projects/{id}/sources", what: "List uploaded sources and their processing state." },
      { method: "GET", path: "/source-versions/{id}/records", what: "Read back the rows and column names as parsed." },
    ],
  },
  {
    id: "api-generate",
    title: "Mapping and generation",
    blurb:
      "Map each value to a column, then run the batch. Three sample rows are rendered and "
      + "checked before the rest are attempted, so a bad mapping costs three documents.",
    endpoints: [
      { method: "GET", path: "/template-manifests/{id}/binding-suggestions", what: "Ranked column suggestions per value, with a confidence band.", note: "?source_version_id=…" },
      { method: "POST", path: "/template-manifests/{id}/bindings", what: "Save the mapping." },
      { method: "POST", path: "/template-manifests/{id}/generate-batch", what: "Start a batch. Returns 202 with a job_id.", note: "one document per row" },
      { method: "GET", path: "/jobs/{id}", what: "Poll the batch: status, per-row outcome, and any error." },
      { method: "POST", path: "/template-manifests/{id}/preview-row", what: "Render a single row without storing anything." },
    ],
  },
  {
    id: "api-documents",
    title: "Documents",
    blurb:
      "Documents carry two independent states: what the engine and reviewers say about them, "
      + "and where a person has put them in their own process.",
    endpoints: [
      { method: "GET", path: "/projects/{id}/documents", what: "Every document the project has produced." },
      { method: "GET", path: "/documents/{id}", what: "One document, with both status axes and whether it can be downloaded." },
      { method: "PATCH", path: "/documents/{id}/workflow", what: "Move it: work_in_progress, completed or cancelled." },
      { method: "GET", path: "/document-versions/{id}/text", what: "The document's text, run by run, for editing." },
      { method: "POST", path: "/document-versions/{id}:approve", what: "Sign it off. Required before it can be downloaded." },
      { method: "GET", path: "/document-versions/{id}/download", what: "Download it.", note: "?format=docx | pdf" },
      { method: "POST", path: "/documents:download", what: "Several at once, as a zip." },
    ],
  },
  {
    id: "api-review",
    title: "Review",
    blurb:
      "Two queues in one: documents a person has objected to, and values the engine would "
      + "not guess at.",
    endpoints: [
      { method: "GET", path: "/review-queue", what: "Everything waiting for a person, ranked." },
      { method: "POST", path: "/document-versions/{id}:request-changes", what: "Object to a document, with a reason." },
      { method: "POST", path: "/reviews/{id}/comments", what: "Comment on an open review." },
      { method: "POST", path: "/reviews/{id}:approve", what: "Resolve a review." },
      { method: "GET", path: "/review-tasks", what: "Values the engine parked for a person to decide." },
    ],
  },
];

const CONTENTS = [
  { id: "start", label: "What this does", icon: BookOpen },
  { id: "workflow", label: "The five steps", icon: WorkflowIcon },
  { id: "authoring", label: "Marking up a template", icon: FileText },
  { id: "conditions", label: "Optional sections", icon: Braces },
  { id: "states", label: "Document states", icon: Boxes },
  { id: "api-intro", label: "API — getting started", icon: Terminal },
  { id: "api-errors", label: "API — errors", icon: Shield },
  ...API.map((g) => ({ id: g.id, label: `API — ${g.title.toLowerCase()}`, icon: Braces })),
  { id: "recipe", label: "End-to-end example", icon: Terminal },
];

/* ------------------------------------------------------------- fragments */

function Code({ children, lang }: { children: string; lang?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="group relative my-3 overflow-hidden rounded-xl border border-border bg-[color-mix(in_oklab,var(--color-surface)_60%,black_8%)]">
      <div className="flex items-center justify-between border-b border-border/60 px-3 py-1.5">
        <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
          {lang ?? "http"}
        </span>
        <button
          onClick={() => {
            void navigator.clipboard?.writeText(children);
            setCopied(true);
            setTimeout(() => setCopied(false), 1400);
          }}
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          {copied ? <Check className="h-3 w-3 text-success" /> : <Copy className="h-3 w-3" />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="overflow-x-auto px-3.5 py-3 text-[12.5px] leading-relaxed">
        <code className="font-mono text-foreground/90">{children}</code>
      </pre>
    </div>
  );
}

function Section({ id, title, eyebrow, children }: {
  id: string; title: string; eyebrow?: string; children: React.ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-24">
      {eyebrow && (
        <div className="mb-1 font-mono text-[11px] uppercase tracking-wider text-brand">{eyebrow}</div>
      )}
      <h2 className="text-xl font-semibold tracking-tight">{title}</h2>
      <div className="mt-3 space-y-3 text-[14px] leading-relaxed text-muted-foreground">
        {children}
      </div>
    </section>
  );
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <div className="relative pl-11">
      <div className="absolute left-0 top-0 flex h-7 w-7 items-center justify-center rounded-full bg-gradient-brand font-mono text-xs font-semibold text-white">
        {n}
      </div>
      <div className="text-[14px] font-medium text-foreground">{title}</div>
      <div className="mt-1 text-[13.5px] leading-relaxed text-muted-foreground">{children}</div>
    </div>
  );
}

function Swatch({ colour, name, means, hexes }: {
  colour: string; name: string; means: string; hexes: string;
}) {
  return (
    <div className="flex items-start gap-3 rounded-xl border border-border bg-background/40 p-3">
      <span className="mt-0.5 h-4 w-4 shrink-0 rounded" style={{ background: colour }} />
      <div className="min-w-0">
        <div className="text-[13.5px] font-medium text-foreground">{name}</div>
        <div className="text-[13px] text-muted-foreground">{means}</div>
        <div className="mt-0.5 font-mono text-[11px] text-muted-foreground/70">{hexes}</div>
      </div>
    </div>
  );
}

function EndpointRow({ e }: { e: Endpoint }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-border/60 py-2.5 last:border-0">
      <span className={cn("shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[10px] font-semibold",
                          METHOD_TONE[e.method])}>
        {e.method}
      </span>
      <code className="font-mono text-[12.5px] text-foreground">{e.path}</code>
      {e.note && (
        <span className="font-mono text-[11px] text-muted-foreground/70">{e.note}</span>
      )}
      <span className="w-full text-[13px] text-muted-foreground sm:w-auto sm:flex-1">{e.what}</span>
    </div>
  );
}

/* ------------------------------------------------------------------ page */

function DocsPage() {
  const [active, setActive] = useState(CONTENTS[0].id);
  const [q, setQ] = useState("");
  const contentRef = useRef<HTMLDivElement>(null);

  // Highlight whichever section the reader is actually looking at. Keyed on the
  // topmost heading that has crossed the fold rather than the first one merely
  // intersecting, which flickers between two neighbours on a slow scroll.
  useEffect(() => {
    const onScroll = () => {
      let current = CONTENTS[0].id;
      for (const c of CONTENTS) {
        const el = document.getElementById(c.id);
        if (el && el.getBoundingClientRect().top <= 120) current = c.id;
      }
      setActive(current);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  const contents = useMemo(
    () => CONTENTS.filter((c) => c.label.toLowerCase().includes(q.toLowerCase())),
    [q],
  );

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* The marketing header, not the application shell: this page is public. */}
      <header className="sticky top-0 z-30 border-b border-border bg-background/70 backdrop-blur-md backdrop-saturate-150">
        <div className="mx-auto flex h-16 max-w-[1400px] items-center justify-between px-6 lg:px-8">
          <Link to="/" className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-brand">
              <Sparkles className="h-4 w-4 text-white" />
            </div>
            <span className="font-semibold">DocuMind AI</span>
            <span className="ml-1 rounded-md border border-border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
              Docs
            </span>
          </Link>
          <nav className="hidden items-center gap-7 text-sm text-muted-foreground md:flex">
            <a href="#workflow" className="hover:text-foreground">Using it</a>
            <a href="#authoring" className="hover:text-foreground">Templates</a>
            <a href="#api-intro" className="hover:text-foreground">API</a>
          </nav>
          <div className="flex items-center gap-2">
            <Link to="/" className="px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground">
              Home
            </Link>
            <Link to="/dashboard"
                  className="rounded-lg bg-gradient-brand px-4 py-2 text-sm font-medium text-white hover:opacity-90">
              Sign in
            </Link>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-[1400px] px-6 py-8 lg:px-8">
      <FadeIn className="relative mb-8 overflow-hidden rounded-2xl surface-raised p-8 md:p-10">
        <div aria-hidden className="pointer-events-none absolute inset-0 bg-hero-orbs opacity-60" />
        <div className="relative">
          <div className="font-mono text-[11px] uppercase tracking-wider text-brand">Documentation</div>
          <h1 className="mt-2 text-4xl font-bold tracking-tight text-gradient md:text-5xl">
            Fill a Word template from a spreadsheet
          </h1>
          <p className="mt-3 max-w-2xl text-[15px] leading-relaxed text-muted-foreground">
            Upload the letter you already send. Mark which words are values. Give it your data.
            Every row becomes a document, checked before it reaches anybody. This page covers
            using it, and the HTTP API for driving it from your own systems.
          </p>
        </div>
      </FadeIn>

      <div className="grid grid-cols-1 gap-10 lg:grid-cols-[240px_1fr]">
        {/* contents */}
        <aside className="hidden lg:block">
          <div className="sticky top-24 space-y-3">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Filter"
                className="h-8 w-full rounded-lg border border-border bg-surface pl-8 pr-2 text-[13px] placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring/40"
              />
            </div>
            <nav className="space-y-0.5">
              {contents.map((c) => (
                <a
                  key={c.id}
                  href={`#${c.id}`}
                  className={cn(
                    "flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-[13px] transition-colors",
                    active === c.id
                      ? "bg-brand/10 font-medium text-foreground"
                      : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
                  )}
                >
                  <c.icon className={cn("h-3.5 w-3.5 shrink-0", active === c.id && "text-brand")} />
                  <span className="truncate">{c.label}</span>
                </a>
              ))}
            </nav>
          </div>
        </aside>

        {/* content */}
        <div ref={contentRef} className="min-w-0 space-y-12 pb-24">
          <Section id="start" eyebrow="Start here" title="What this does">
            <p>
              You have a letter you send often — an offer, a contract, a change of terms — and a
              spreadsheet of people to send it to. This fills one for each row, keeping your
              letterhead, tables and formatting exactly as they are.
            </p>
            <p>
              Nothing is rewritten by a model at generation time. The template decides the words;
              your data decides the values. What a model does do is <em>read</em> the template
              once, to work out which words are values.
            </p>
          </Section>

          <Section id="workflow" eyebrow="Using it" title="The five steps">
            <div className="mt-2 space-y-5">
              <Step n={1} title="Create a project">
                A project is one letter type for one purpose. Templates, data and documents all
                live inside it.
              </Step>
              <Step n={2} title="Upload your template">
                A <code className="font-mono text-[12px]">.docx</code>. It is read as it lands —
                the row tells you how many values and conditions were found, and names anything it
                could not account for. Fix those before going on.
              </Step>
              <Step n={3} title="Get the spreadsheet and fill it in">
                Download the data template. It has one column per value, named after it, and a
                fixed list of choices on any column that decides which sections appear. Fill a row
                per person and upload it.
              </Step>
              <Step n={4} title="Map the columns, then generate">
                The columns are matched to values for you; confirm them and run the batch. Three
                sample documents are rendered and checked first — if they fail, the rest are not
                attempted.
              </Step>
              <Step n={5} title="Review, approve, download">
                Documents appear as they are produced. Move each through your own workflow, send
                any you disagree with to review, and approve the ones that are right. Only an
                approved document can be downloaded.
              </Step>
            </div>
          </Section>

          <Section id="authoring" eyebrow="Templates" title="Marking up a template">
            <p>
              Every piece of text in your template is one of three things, and you say which by
              colouring it. This is the whole convention.
            </p>
            <div className="mt-3 grid gap-2 sm:grid-cols-3">
              <Swatch colour="#0070C0" name="Blue — a value"
                      means="Replaced with data. One blue item becomes one spreadsheet column."
                      hexes="0000FF · 0070C0 · 0563C1" />
              <Swatch colour="#FF0000" name="Red — an instruction"
                      means="A note to the system or the preparer. Removed before sending."
                      hexes="FF0000 · C00000 · E00000" />
              <Swatch colour="#3C3C3C" name="Black — the letter"
                      means="Copied out exactly. Anything black will be printed."
                      hexes="everything else" />
            </div>
            <p className="pt-1">
              Write a value as its name in angle brackets and colour it blue:{" "}
              <code className="rounded bg-info/10 px-1 py-0.5 font-mono text-[12px] text-info">
                &lt;Base Salary&gt;
              </code>. Type it in one go — Word splits text when formatting changes mid-word, and a
              placeholder split in half can no longer be filled.
            </p>
            <p>
              If your documents already use the highlighter instead of font colour, a{" "}
              <span className="rounded px-1" style={{ background: "#FFFF00", color: "#000" }}>yellow highlight</span>{" "}
              is read as blue and a{" "}
              <span className="rounded px-1" style={{ background: "#00FF00", color: "#000" }}>bright green highlight</span>{" "}
              as red.
            </p>
            <p className="rounded-xl border border-warning/30 bg-warning/5 p-3 text-[13.5px]">
              <strong className="text-warning">Never draw a blank.</strong> A slot written as{" "}
              <code className="font-mono text-[12px]">__/__/____</code> or{" "}
              <code className="font-mono text-[12px]">XX months</code> has nothing to attach data
              to, so it stays exactly as you typed it. Name it instead. One date is one
              placeholder — not three.
            </p>
          </Section>

          <Section id="conditions" eyebrow="Templates" title="Optional sections">
            <p>
              Text that should appear only sometimes goes between markers. Colour the markers red
              so they are removed either way.
            </p>
            <Code lang="template">{`[[IF employment_basis == 'Part Time']]
You will work on a part-time basis as set out in Schedule 1.
[[ELSE]]
You will work on a full-time basis.
[[ENDIF]]`}</Code>
            <p>
              <code className="font-mono text-[12px]">[[ELSE]]</code> is optional, and markers can
              sit inline within a sentence. What you can write in a condition:
            </p>
            <Code lang="conditions">{`name == 'Value'          name != 'Value'
salary > 100000          hours <= 38
country in ('AU','NZ')   country not in ('AU','NZ')
bonus is blank           address_line_2 is not blank
type == 'Fixed' AND country == 'NZ'`}</Code>
            <p>
              Use the name of a blue value on the left, in lower case with underscores instead of
              spaces. Angle brackets are for values only — never for optional wording. A phrase
              like <code className="font-mono text-[12px]">&lt;on a part time basis&gt;</code> is a
              condition, not a value, and will be rejected.
            </p>
          </Section>

          <Section id="states" eyebrow="Using it" title="Document states">
            <p>
              A document carries two states at once, because they answer different questions and
              collapsing them loses one.
            </p>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-xl border border-border bg-background/40 p-3.5">
                <div className="text-[13.5px] font-medium text-foreground">What the system says</div>
                <p className="mt-1 text-[13px]">
                  Draft, awaiting a decision, changes requested, failed QA, approved. Set by the
                  engine and by reviewers — not by hand.
                </p>
              </div>
              <div className="rounded-xl border border-border bg-background/40 p-3.5">
                <div className="text-[13.5px] font-medium text-foreground">Where you have put it</div>
                <p className="mt-1 text-[13px]">
                  Work in progress, completed, cancelled. Yours to set. Approved and blocked appear
                  here too but cannot be chosen — one is a signature, the other a QA verdict.
                </p>
              </div>
            </div>
            <p>
              <strong className="text-foreground">Only an approved document can be downloaded.</strong>{" "}
              That is enforced by the server on every route, not just hidden in the interface.
            </p>
          </Section>

          {/* ---------------------------------------------------------- API */}
          <Section id="api-intro" eyebrow="API" title="Getting started">
            <p>
              Everything the interface does is available over HTTP. The base URL is your
              deployment followed by <code className="font-mono text-[12px]">/api/v1</code>.
            </p>
            <Code lang="bash">{`# 1 — get a token
curl -X POST https://your-host/api/v1/auth/token \\
  -H 'Content-Type: application/json' \\
  -d '{"email":"you@example.com","password":"…"}'

# → {"access_token":"eyJ…","token_type":"bearer"}

# 2 — use it on every call
curl https://your-host/api/v1/projects \\
  -H "Authorization: Bearer $TOKEN"`}</Code>
            <p className="rounded-xl border border-info/25 bg-info/5 p-3 text-[13.5px]">
              Tokens expire. A long job — reading a large template can take minutes — will outlive
              one, so mint a fresh token per request or refresh on a timer rather than holding one
              for the life of a run.
            </p>
            <p>
              Uploads are <code className="font-mono text-[12px]">multipart/form-data</code> with
              the file under the field name <code className="font-mono text-[12px]">file</code>.
              Everything else is JSON. Times are ISO 8601. Every response is scoped to your
              organisation; anything belonging to another is a 404 rather than a 403, so a probe
              learns nothing.
            </p>
          </Section>

          <Section id="api-errors" eyebrow="API" title="Errors">
            <p>Failures carry a stable code and a sentence written for a person.</p>
            <Code lang="json">{`{
  "detail": {
    "error": {
      "code": "DOCUMENT_NOT_APPROVED",
      "message": "This document has not been approved, so it cannot be downloaded…",
      "details": { "document_id": "…", "status": "draft" }
    }
  }
}`}</Code>
            <p>
              Branch on <code className="font-mono text-[12px]">code</code>, never on the message —
              the wording is improved over time, the code is not. Common ones:
            </p>
            <div className="rounded-xl border border-border">
              {[
                ["MANIFEST_NOT_READ", "409", "The template could not be read; there is nothing to fill from."],
                ["MANIFEST_RETIRED", "409", "A newer reading of this template has replaced this one."],
                ["DOCUMENT_NOT_APPROVED", "409", "Downloading requires approval."],
                ["DOCUMENT_BLOCKED", "409", "The document failed its checks and cannot be approved."],
                ["SOURCE_HAS_NO_ROWS", "422", "The spreadsheet has headers but no data."],
                ["CAPABILITY_REQUIRED", "403", "Your role cannot do this."],
                ["TOKEN_EXPIRED", "401", "Mint a new token."],
              ].map(([code, status, what]) => (
                <div key={code} className="flex flex-wrap items-baseline gap-x-3 border-b border-border/60 px-3 py-2 last:border-0">
                  <code className="font-mono text-[12px] text-foreground">{code}</code>
                  <span className="font-mono text-[11px] text-muted-foreground">{status}</span>
                  <span className="w-full text-[13px] sm:w-auto sm:flex-1">{what}</span>
                </div>
              ))}
            </div>
          </Section>

          {API.map((g) => (
            <Section key={g.id} id={g.id} eyebrow="API reference" title={g.title}>
              <p>{g.blurb}</p>
              <div className="rounded-xl border border-border px-3">
                {g.endpoints.map((e) => <EndpointRow key={e.method + e.path} e={e} />)}
              </div>
            </Section>
          ))}

          <Section id="recipe" eyebrow="API" title="End-to-end example">
            <p>
              Letters from your own system, start to finish. Each step waits for the one before —
              reading a template and running a batch are both asynchronous in effect, so poll
              rather than assume.
            </p>
            <Code lang="bash">{`BASE=https://your-host/api/v1
AUTH="Authorization: Bearer $TOKEN"

# 1 — a project
PID=$(curl -s -X POST $BASE/projects -H "$AUTH" -H 'Content-Type: application/json' \\
  -d '{"name":"Offer letters","region":"Asia Pacific",
       "function":"Human Resources","document_type":"HR Letters"}' | jq -r .id)

# 2 — upload the template. This reads it too.
TID=$(curl -s -X POST $BASE/projects/$PID/templates -H "$AUTH" \\
  -F file=@offer-letter.docx | jq -r .id)

MID=$(curl -s $BASE/templates/$TID/manifests -H "$AUTH" | jq -r '.items[0].id')

# 3 — the spreadsheet this template wants, then fill and upload it
curl -s $BASE/template-manifests/$MID/source-template -H "$AUTH" -o data.xlsx
#    … fill data.xlsx, one row per person …
SV=$(curl -s -X POST $BASE/projects/$PID/sources -H "$AUTH" \\
  -F file=@data.xlsx | jq -r .current_version_id)

# 4 — map the columns and save
curl -s "$BASE/template-manifests/$MID/binding-suggestions?source_version_id=$SV" \\
  -H "$AUTH" | jq '{source_version_id:"'$SV'",
      field_bindings:[.suggestions[]|select(.column)|{key:.field_id,value:.column}]|from_entries}' \\
  > bindings.json
curl -s -X POST $BASE/template-manifests/$MID/bindings -H "$AUTH" \\
  -H 'Content-Type: application/json' -d @bindings.json

# 5 — generate, then poll
JOB=$(curl -s -X POST $BASE/template-manifests/$MID/generate-batch -H "$AUTH" \\
  -H 'Content-Type: application/json' \\
  -d '{"source_version_id":"'$SV'","language":"en"}' | jq -r .job_id)

until [ "$(curl -s $BASE/jobs/$JOB -H "$AUTH" | jq -r .status)" != "running" ]; do sleep 2; done

# 6 — approve, then download
for V in $(curl -s $BASE/projects/$PID/documents -H "$AUTH" | jq -r '.items[].current_version_id'); do
  curl -s -X POST $BASE/document-versions/$V:approve -H "$AUTH" > /dev/null
  curl -s "$BASE/document-versions/$V/download?format=docx" -H "$AUTH" -o "$V.docx"
done`}</Code>
            <p className="rounded-xl border border-border bg-background/40 p-3 text-[13.5px]">
              Step 3 is the one worth keeping. Asking the template for its own spreadsheet means
              the column names match the values exactly, so nothing has to be guessed. Bind your
              own export instead and the matching becomes inference — which works, but is the part
              most worth checking.
            </p>
          </Section>

          <div className="flex items-center gap-2 border-t border-border pt-6 text-[13px] text-muted-foreground">
            <ChevronRight className="h-3.5 w-3.5" />
            The full machine-readable schema is at{" "}
            <code className="font-mono text-[12px]">/openapi.json</code> on your deployment.
          </div>
        </div>
      </div>
      </div>
    </div>
  );
}
