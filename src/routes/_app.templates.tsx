import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { FileText, Search, Plus, Star, Wand2, Braces, Sparkles, GitBranch, Repeat } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { TemplateEditor } from "@/components/template-editor";
import { TemplateConversionWizard } from "@/components/template-conversion-wizard";
import { api } from "@/lib/api";
import { toast } from "sonner";

export const Route = createFileRoute("/_app/templates")({
  head: () => ({
    meta: [
      { title: "Templates — DocuMind AI" },
      { name: "description", content: "Author token-based templates: black for static text, blue for source values, red for AI prompts. Import .docx or paste raw text." },
      { property: "og:title", content: "Templates authoring — DocuMind AI" },
      { property: "og:description", content: "Build reusable AI document templates with color-coded tokens." },
    ],
  }),
  component: TemplatesPage,
});

const CATEGORIES = ["All", "HR", "Clinical", "Quality-CMC", "Medical Affairs", "Marketing", "Legal"];

type Template = {
  id: string;
  name: string;
  category: string;
  description: string | null;
  starred: boolean;
  uses: number;
  version: string;
};

function TemplatesPage() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [cat, setCat] = useState("All");
  const [selectedId, setSelectedId] = useState<string>("");
  const [importOpen, setImportOpen] = useState(false);

  const refresh = () => {
    api.listLibrary()
      .then((r) => {
        setTemplates(r.items);
        if (!selectedId && r.items[0]) setSelectedId(r.items[0].id);
      })
      .catch((e: any) => toast.error("Could not load templates", { description: e?.message ?? String(e) }))
      .finally(() => setLoading(false));
  };

  useEffect(() => { refresh(); }, []);

  const list = useMemo(
    () => templates.filter(
      (t) => (cat === "All" || t.category === cat) && t.name.toLowerCase().includes(q.toLowerCase()),
    ),
    [templates, cat, q],
  );

  const selected = templates.find((t) => t.id === selectedId) ?? list[0];

  const createBlank = () => {
    api.createLibraryEntry({ name: "Untitled template", category: "HR", content_html: "<h1>Untitled template</h1><p>Start writing…</p>" })
      .then((tpl) => {
        setTemplates((s) => [tpl, ...s]);
        setSelectedId(tpl.id);
        toast.success("New template created");
      })
      .catch((e: any) => toast.error("Could not create template", { description: e?.message ?? String(e) }));
  };

  const handleConvert = (r: { name: string; category: string; html: string }) => {
    api.createLibraryEntry({ name: r.name, category: r.category, content_html: r.html })
      .then((tpl) => {
        setTemplates((s) => [tpl, ...s]);
        setSelectedId(tpl.id);
        toast.success("Template converted", { description: r.name });
      })
      .catch((e: any) => toast.error("Could not save converted template", { description: e?.message ?? String(e) }));
  };

  return (
    <div className="p-6 lg:p-8 max-w-[1400px] mx-auto space-y-5">
      <div className="flex items-start justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Templates</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Build templates from color-coded tokens. Static text stays black; blue pulls from source data; red is an LLM prompt.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => setImportOpen(true)} className="gap-2">
            <Wand2 className="h-4 w-4" /> Convert legacy template
          </Button>
          <Button onClick={createBlank} className="gap-2">
            <Plus className="h-4 w-4" /> New template
          </Button>
        </div>
      </div>

      {/* Legend row */}
      <div className="rounded-xl border border-border bg-card p-4 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <span className="font-medium text-muted-foreground text-xs uppercase tracking-wider mr-1">Token legend</span>
        <LegendChip label="Static (black)" cssVar="--color-foreground" glyph="Aa" />
        <LegendChip label="Source value (blue)" cssVar="--color-token-source" glyph="{ }" Icon={Braces} />
        <LegendChip label="LLM prompt (red)" cssVar="--color-token-prompt" glyph="⚡" Icon={Sparkles} />
        <LegendChip label="Conditional (green)" cssVar="--color-token-conditional" glyph="⌥" Icon={GitBranch} />
        <LegendChip label="Repeat (purple)" cssVar="--color-token-repeat" glyph="↻" Icon={Repeat} />
      </div>

      {/* Category chips + search */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[220px] max-w-sm">
          <Search className="h-4 w-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search templates…" className="pl-9" />
        </div>
        <div className="flex flex-wrap gap-1.5">
          {CATEGORIES.map((c) => (
            <button
              key={c}
              onClick={() => setCat(c)}
              className={cn(
                "px-2.5 py-1 rounded-full text-xs font-medium border transition-colors",
                cat === c ? "bg-primary text-primary-foreground border-primary" : "border-border hover:bg-muted",
              )}
            >
              {c}
            </button>
          ))}
        </div>
      </div>

      {/* Split: list + editor */}
      <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-5">
        {/* Left list */}
        <div className="rounded-xl border border-border bg-card overflow-hidden flex flex-col max-h-[calc(100vh-260px)]">
          <div className="px-4 py-3 border-b border-border text-xs uppercase tracking-wider text-muted-foreground">
            {list.length} template{list.length !== 1 && "s"}
          </div>
          <div className="flex-1 overflow-auto divide-y divide-border">
            {list.map((t) => (
              <button
                key={t.id}
                onClick={() => setSelectedId(t.id)}
                className={cn(
                  "w-full text-left px-4 py-3 hover:bg-accent/50 transition-colors flex gap-3 items-start",
                  selected?.id === t.id && "bg-accent/70",
                )}
              >
                <div className="h-9 w-9 rounded-lg bg-gradient-to-br from-primary/20 to-purple-500/20 flex items-center justify-center shrink-0">
                  <FileText className="h-4 w-4 text-primary" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <div className="font-medium text-sm truncate">{t.name}</div>
                    {t.starred && <Star className="h-3 w-3 fill-yellow-500 text-yellow-500 shrink-0" />}
                  </div>
                  <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                    <Badge variant="secondary" className="text-[10px] px-1.5 py-0">{t.category}</Badge>
                    <Badge variant="outline" className="text-[10px] px-1.5 py-0">{t.version}</Badge>
                    <span className="text-[11px] text-muted-foreground">· {t.uses} uses</span>
                  </div>
                </div>
              </button>
            ))}
            {!loading && list.length === 0 && (
              <div className="p-6 text-center text-sm text-muted-foreground">No templates match.</div>
            )}
          </div>
        </div>

        {/* Right editor */}
        <div className="rounded-xl border border-border bg-card overflow-hidden flex flex-col min-h-[70vh] max-h-[calc(100vh-160px)]">
          {selected ? (
            <TemplateEditor
              key={selected.id}
              templateId={selected.id}
              templateName={selected.name}
            />
          ) : (
            <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm">
              {loading ? "Loading templates…" : "Select a template to start editing."}
            </div>
          )}
        </div>
      </div>

      <TemplateConversionWizard open={importOpen} onOpenChange={setImportOpen} onFinish={handleConvert} />
    </div>
  );
}

function LegendChip({ label, cssVar, glyph, Icon }: { label: string; cssVar: string; glyph: string; Icon?: any }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs">
      <span
        className="inline-flex items-center justify-center h-5 min-w-[24px] px-1 rounded font-mono font-bold text-[10px]"
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
