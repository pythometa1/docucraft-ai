import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Braces,
  FileText,
  GitBranch,
  Loader2,
  Plus,
  Search,
  Sparkles,
  Wand2,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { BlueprintImportDialog } from "@/components/blueprint-import-dialog";
import { BlueprintKitDialog } from "@/components/blueprint-kit-dialog";
import { FadeIn, Stagger, StaggerItem } from "@/components/motion";
import type { Blueprint } from "@/lib/types";

export const Route = createFileRoute("/_app/templates")({
  head: () => ({
    meta: [
      { title: "Templates — DocuMind AI" },
      { name: "description", content: "Read a legacy .docx into an editable template, correct what the engine understood, and publish it as something the fill engine can execute." },
      { property: "og:title", content: "Template authoring — DocuMind AI" },
      { property: "og:description", content: "Legacy template in, mappable template out." },
    ],
  }),
  component: TemplatesPage,
});

function TemplatesPage() {
  const navigate = useNavigate();
  const [blueprints, setBlueprints] = useState<Blueprint[]>([]);
  const [legacy, setLegacy] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [kitOpen, setKitOpen] = useState(false);
  const [migrating, setMigrating] = useState<string | null>(null);

  const refresh = () => {
    setLoading(true);
    Promise.all([
      api.listBlueprints().catch(() => ({ items: [] as Blueprint[] })),
      // The old token library. Still readable so nobody's authoring work
      // disappears, and no longer the way new templates are made.
      api.listLibrary().catch(() => ({ items: [] as any[] })),
    ])
      .then(([bp, lib]) => { setBlueprints(bp.items); setLegacy(lib.items ?? []); })
      .catch((e: any) => toast.error("Could not load templates", { description: e?.message }))
      .finally(() => setLoading(false));
  };

  useEffect(refresh, []);

  const list = useMemo(
    () => blueprints.filter((b) => b.name.toLowerCase().includes(q.toLowerCase())),
    [blueprints, q],
  );

  return (
    <div className="mx-auto max-w-[1400px] space-y-5 p-6 lg:p-8">
      <FadeIn className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-gradient">Templates</h1>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            Put a legacy <code className="text-xs">.docx</code> in and it is pre-scanned and
            compiled: placeholders, author instructions and conditional sections are identified for
            you. Correct what it got wrong, take the document back, and publish it as a template the
            fill engine can execute.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => setKitOpen(true)} className="gap-2">
            <Plus className="h-4 w-4" /> Start from scratch
          </Button>
          <Button onClick={() => setImportOpen(true)} className="gap-2 sheen">
            <span aria-hidden className="sheen-layer" />
            <Wand2 className="h-4 w-4" /> Read a legacy template
          </Button>
        </div>
      </FadeIn>

      {/* What the colours mean — the same three the pre-scanner classifies runs into. */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-xl surface-raised p-4 text-sm">
        <span className="mr-1 text-xs font-medium uppercase tracking-wider text-muted-foreground">
          How a template is read
        </span>
        <Legend label="Static text — copied as written" cssVar="--color-foreground" glyph="Aa" />
        <Legend label="Placeholder — filled from your data" cssVar="--color-token-source" Icon={Braces} />
        <Legend label="Author instruction — removed from the letter" cssVar="--color-token-prompt" Icon={Sparkles} />
        <Legend label="Conditional section" cssVar="--color-token-conditional" Icon={GitBranch} />
      </div>

      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search templates…" className="pl-9" />
      </div>

      {loading ? (
        <div className="py-16 text-center text-sm text-muted-foreground">Loading templates…</div>
      ) : list.length === 0 ? (
        <EmptyState onImport={() => setImportOpen(true)} filtered={q.length > 0} />
      ) : (
        <Stagger className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {list.map((b) => (
            <StaggerItem key={b.id}>
            <button
              onClick={() => navigate({ to: "/templates/$blueprintId", params: { blueprintId: b.id } })}
              className="group h-full w-full rounded-xl surface-raised p-4 text-left transition-all duration-200 hover:-translate-y-0.5 hover:border-primary/50 hover:glow-soft"
            >
              <div className="flex items-start gap-3">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-primary/25 to-purple/25 transition-transform duration-200 group-hover:scale-105">
                  <FileText className="h-4 w-4 text-primary" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium">{b.name}</div>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                    <Badge variant={b.status === "published" ? "default" : "secondary"} className="px-1.5 py-0 text-[10px]">
                      {b.status}
                    </Badge>
                    <Badge variant="outline" className="px-1.5 py-0 text-[10px]">v{b.version_no ?? 1}</Badge>
                    <span className="text-[11px] text-muted-foreground">· read from a {b.kind} source</span>
                  </div>
                </div>
              </div>
            </button>
            </StaggerItem>
          ))}
        </Stagger>
      )}

      {legacy.length > 0 && (
        <div className="rounded-xl border border-border bg-muted/30 p-4">
          <p className="mb-1 flex items-center gap-1.5 text-sm font-medium">
            <AlertTriangle className="h-3.5 w-3.5 text-amber-500" />
            {legacy.length} template{legacy.length === 1 ? "" : "s"} in the old token library
          </p>
          <p className="text-xs text-muted-foreground">
            These were authored as coloured tokens, which produced HTML that no manifest could be
            compiled from — so they could never actually fill a document. Migrating one turns its
            tokens into placeholders and conditions; nothing is deleted, the entry stays where it is.
          </p>
          <div className="mt-3 space-y-1.5">
            {legacy.map((t: any) => (
              <div key={t.id} className="flex items-center justify-between gap-3 rounded-lg border border-border bg-background/60 px-3 py-2">
                <span className="truncate text-sm">{t.name}</span>
                <Button
                  size="sm" variant="ghost" className="gap-1.5 shrink-0"
                  disabled={migrating != null}
                  onClick={async () => {
                    setMigrating(t.id);
                    try {
                      const bp = await api.blueprintFromLibrary({ library_id: t.id });
                      toast.success("Migrated", { description: t.name });
                      navigate({ to: "/templates/$blueprintId", params: { blueprintId: bp.id } });
                    } catch (e: any) {
                      toast.error("Could not migrate", { description: e?.message ?? String(e) });
                    } finally { setMigrating(null); }
                  }}
                >
                  {migrating === t.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ArrowRight className="h-3.5 w-3.5" />}
                  Migrate
                </Button>
              </div>
            ))}
          </div>
        </div>
      )}

      <BlueprintKitDialog
        open={kitOpen}
        onOpenChange={setKitOpen}
        onCreated={(id) => navigate({ to: "/templates/$blueprintId", params: { blueprintId: id } })}
      />

      <BlueprintImportDialog
        open={importOpen}
        onOpenChange={setImportOpen}
        onCreated={(id) => navigate({ to: "/templates/$blueprintId", params: { blueprintId: id } })}
      />
    </div>
  );
}

function EmptyState({ onImport, filtered }: { onImport: () => void; filtered: boolean }) {
  if (filtered) {
    return <div className="py-16 text-center text-sm text-muted-foreground">No templates match.</div>;
  }
  return (
    <div className="rounded-xl border border-dashed border-border py-16 text-center">
      <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-gradient-to-br from-primary/20 to-purple-500/20">
        <Wand2 className="h-5 w-5 text-primary" />
      </div>
      <p className="font-medium">No templates yet</p>
      <p className="mx-auto mt-1 max-w-md text-sm text-muted-foreground">
        Start with a Word document you already send — an offer letter, a contract, a study report.
        It is read as it is, brackets and coloured instructions and all.
      </p>
      <Button onClick={onImport} className="mt-4 gap-2">
        <Wand2 className="h-4 w-4" /> Read a legacy template
      </Button>
    </div>
  );
}

function Legend({ label, cssVar, glyph, Icon }: {
  label: string; cssVar: string; glyph?: string; Icon?: any;
}) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs">
      <span
        className="inline-flex h-5 min-w-[24px] items-center justify-center rounded px-1 font-mono text-[10px] font-bold"
        style={{
          color: `var(${cssVar})`,
          background: `color-mix(in oklab, var(${cssVar}) 14%, transparent)`,
          border: `1px solid color-mix(in oklab, var(${cssVar}) 35%, transparent)`,
        }}
      >
        {Icon ? <Icon className="h-3 w-3" /> : glyph}
      </span>
      <span className="text-muted-foreground">{label}</span>
    </span>
  );
}
