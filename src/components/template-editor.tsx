import { useEffect, useState } from "react";
import { EditorContent, useEditor, type Editor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Underline from "@tiptap/extension-underline";
import Placeholder from "@tiptap/extension-placeholder";
import { Node, mergeAttributes } from "@tiptap/core";
import {
  Bold, Italic, Underline as UnderlineIcon, Heading1, Heading2, Heading3,
  List, ListOrdered, Quote, Minus, Undo2, Redo2, Type, Save,
  Braces, Sparkles, GitBranch, Repeat, Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { api } from "@/lib/api";

/* ------------------------- Token nodes ------------------------- */

const SourceToken = Node.create({
  name: "sourceToken",
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,
  addAttributes() {
    return {
      field: { default: "full_name" },
      fallback: { default: "" },
    };
  },
  parseHTML() { return [{ tag: "span[data-token='source']" }]; },
  renderHTML({ HTMLAttributes }) {
    return ["span", mergeAttributes(HTMLAttributes, {
      "data-token": "source",
      class: "tpl-token tpl-token-source",
    }), `{${HTMLAttributes.field ?? "field"}}`];
  },
});

const PromptToken = Node.create({
  name: "promptToken",
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,
  addAttributes() {
    return { prompt: { default: "Describe what the LLM should generate here" } };
  },
  parseHTML() { return [{ tag: "span[data-token='prompt']" }]; },
  renderHTML({ HTMLAttributes }) {
    const text = HTMLAttributes.prompt ?? "";
    const short = text.length > 60 ? text.slice(0, 60) + "…" : text;
    return ["span", mergeAttributes(HTMLAttributes, {
      "data-token": "prompt",
      class: "tpl-token tpl-token-prompt",
    }), `⚡ ${short}`];
  },
});

const ConditionalToken = Node.create({
  name: "conditionalToken",
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,
  addAttributes() {
    return {
      condition: { default: "region == 'EU'" },
      body: { default: "GDPR clause applies." },
    };
  },
  parseHTML() { return [{ tag: "span[data-token='conditional']" }]; },
  renderHTML({ HTMLAttributes }) {
    return ["span", mergeAttributes(HTMLAttributes, {
      "data-token": "conditional",
      class: "tpl-token tpl-token-conditional",
    }), `⌥ if ${HTMLAttributes.condition ?? ""}`];
  },
});

const RepeatToken = Node.create({
  name: "repeatToken",
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,
  addAttributes() {
    return {
      variable: { default: "item" },
      collection: { default: "items" },
      body: { default: "• {item}" },
    };
  },
  parseHTML() { return [{ tag: "span[data-token='repeat']" }]; },
  renderHTML({ HTMLAttributes }) {
    return ["span", mergeAttributes(HTMLAttributes, {
      "data-token": "repeat",
      class: "tpl-token tpl-token-repeat",
    }), `↻ for ${HTMLAttributes.variable} in ${HTMLAttributes.collection}`];
  },
});

/* ------------------------- Editor ------------------------- */

export const TOKEN_COLORS = {
  static: { label: "Static text", var: "--color-foreground", icon: Type, glyph: "Aa" },
  source: { label: "Source value", var: "--color-token-source", icon: Braces, glyph: "{ }" },
  prompt: { label: "LLM prompt", var: "--color-token-prompt", icon: Sparkles, glyph: "⚡" },
  conditional: { label: "Conditional", var: "--color-token-conditional", icon: GitBranch, glyph: "⌥" },
  repeat: { label: "Repeat / loop", var: "--color-token-repeat", icon: Repeat, glyph: "↻" },
} as const;

const DEFAULT_SOURCE_FIELDS = [
  "full_name", "first_name", "last_name", "email", "role", "department",
  "start_date", "end_date", "salary", "region", "country", "manager_name",
];

type Props = {
  templateId: string;
  templateName: string;
  onSaved?: () => void;
};

export function TemplateEditor({ templateId, templateName, onSaved }: Props) {
  const [loadedHtml, setLoadedHtml] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    setLoadedHtml(null);
    api.getLibraryContent(templateId)
      .then((r) => setLoadedHtml(r.content_html || DEFAULT_TEMPLATE_HTML))
      .catch((e: any) => setLoadError(e?.message ?? String(e)));
  }, [templateId]);

  if (loadError) {
    return <div className="flex-1 flex items-center justify-center text-sm text-destructive p-6 text-center">{loadError}</div>;
  }
  if (loadedHtml === null) {
    return <div className="flex-1 flex items-center justify-center text-sm text-muted-foreground">Loading template…</div>;
  }
  return <TemplateEditorInner key={templateId} templateId={templateId} templateName={templateName} initialHtml={loadedHtml} onSaved={onSaved} />;
}

function TemplateEditorInner({ templateId, templateName, initialHtml, onSaved }: Props & { initialHtml: string }) {
  const [dirty, setDirty] = useState(false);
  const [selection, setSelection] = useState<null | {
    type: "source" | "prompt" | "conditional" | "repeat";
    attrs: Record<string, any>;
    pos: number;
  }>(null);

  const editor = useEditor({
    extensions: [
      StarterKit,
      Underline,
      Placeholder.configure({ placeholder: "Start writing your template — mix static text with tokens…" }),
      SourceToken,
      PromptToken,
      ConditionalToken,
      RepeatToken,
    ],
    content: initialHtml,
    editorProps: {
      attributes: {
        class: "tpl-editor min-h-[60vh] focus:outline-none max-w-none text-foreground",
      },
    },
    onUpdate: ({ editor: e }) => {
      setDirty(true);
      refreshSelection(e);
    },
    onSelectionUpdate: ({ editor: e }) => refreshSelection(e),
  });

  function refreshSelection(e: Editor) {
    const node = e.state.selection.$from.nodeAfter ?? e.state.selection.$from.parent;
    const anyNode = e.state.doc.nodeAt(e.state.selection.from);
    const target = anyNode ?? node;
    const map: Record<string, "source" | "prompt" | "conditional" | "repeat"> = {
      sourceToken: "source",
      promptToken: "prompt",
      conditionalToken: "conditional",
      repeatToken: "repeat",
    };
    const t = target && map[target.type.name];
    if (t) {
      setSelection({ type: t, attrs: { ...target.attrs }, pos: e.state.selection.from });
    } else {
      setSelection(null);
    }
  }

  useEffect(() => () => editor?.destroy(), [editor]);

  if (!editor) return null;

  const insertToken = (type: "source" | "prompt" | "conditional" | "repeat") => {
    const nodeName = {
      source: "sourceToken",
      prompt: "promptToken",
      conditional: "conditionalToken",
      repeat: "repeatToken",
    }[type];
    editor.chain().focus().insertContent({ type: nodeName }).run();
    setDirty(true);
  };

  const save = () => {
    api.saveLibraryContent(templateId, editor.getHTML())
      .then(() => {
        setDirty(false);
        toast.success("Template saved");
        onSaved?.();
      })
      .catch((e: any) => toast.error("Could not save", { description: e?.message ?? String(e) }));
  };

  const updateSelectedAttrs = (patch: Record<string, any>) => {
    if (!selection) return;
    const nodeName = {
      source: "sourceToken",
      prompt: "promptToken",
      conditional: "conditionalToken",
      repeat: "repeatToken",
    }[selection.type];
    editor.chain().focus().updateAttributes(nodeName, patch).run();
    setSelection({ ...selection, attrs: { ...selection.attrs, ...patch } });
    setDirty(true);
  };

  const deleteSelected = () => {
    editor.chain().focus().deleteSelection().run();
    setSelection(null);
    setDirty(true);
  };

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Header */}
      <div className="border-b border-border px-5 py-3 flex items-center gap-3 flex-wrap">
        <div className="min-w-0 flex-1">
          <div className="text-xs uppercase tracking-wider text-muted-foreground">Editing template</div>
          <div className="font-semibold truncate">{templateName}</div>
        </div>
        <div className="flex items-center gap-2">
          {dirty ? (
            <span className="text-xs text-warning">Unsaved</span>
          ) : (
            <span className="text-xs text-muted-foreground">Saved</span>
          )}
          <Button size="sm" onClick={save} disabled={!dirty} className="bg-gradient-brand text-white">
            <Save className="h-4 w-4 mr-1.5" /> Save
          </Button>
        </div>
      </div>

      {/* Toolbar */}
      <Toolbar editor={editor} onInsertToken={insertToken} />

      {/* Legend */}
      <div className="px-5 py-2 border-b border-border bg-surface/40 flex flex-wrap items-center gap-3 text-xs">
        <span className="text-muted-foreground font-medium">Legend:</span>
        {(["static", "source", "prompt", "conditional", "repeat"] as const).map((k) => (
          <span key={k} className="inline-flex items-center gap-1.5">
            <span
              className="inline-block h-2.5 w-2.5 rounded-sm"
              style={{ background: `var(${TOKEN_COLORS[k].var})` }}
            />
            <span className="text-muted-foreground">{TOKEN_COLORS[k].label}</span>
          </span>
        ))}
      </div>

      {/* Canvas + Inspector */}
      <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-[1fr_320px] overflow-hidden">
        <div className="overflow-auto bg-background">
          <div className="max-w-3xl mx-auto my-6 rounded-xl border border-border bg-surface shadow-sm">
            <div className="px-10 py-10">
              <EditorContent editor={editor} />
            </div>
          </div>
        </div>
        <Inspector
          selection={selection}
          onChange={updateSelectedAttrs}
          onDelete={deleteSelected}
        />
      </div>

      <style>{`
        .tpl-editor h1 { font-size: 1.75rem; font-weight: 700; line-height: 1.2; margin: 1rem 0 0.6rem; letter-spacing: -0.01em; }
        .tpl-editor h2 { font-size: 1.35rem; font-weight: 600; margin: 1rem 0 0.4rem; }
        .tpl-editor h3 { font-size: 1.1rem; font-weight: 600; margin: 0.8rem 0 0.3rem; }
        .tpl-editor p  { line-height: 1.75; margin: 0.4rem 0; }
        .tpl-editor ul, .tpl-editor ol { padding-left: 1.4rem; margin: 0.4rem 0; }
        .tpl-editor ul { list-style: disc; }
        .tpl-editor ol { list-style: decimal; }
        .tpl-editor li { margin: 0.15rem 0; }
        .tpl-editor blockquote { border-left: 3px solid var(--color-brand); padding: 0.1rem 0 0.1rem 0.9rem; color: var(--color-muted-foreground); margin: 0.6rem 0; font-style: italic; }
        .tpl-editor hr { border: none; border-top: 1px solid var(--color-border); margin: 1rem 0; }
        .tpl-editor strong { font-weight: 700; }
        .tpl-editor em { font-style: italic; }
        .tpl-editor u { text-decoration: underline; }
        .tpl-editor p.is-editor-empty:first-child::before {
          content: attr(data-placeholder);
          color: var(--color-muted-foreground);
          float: left; height: 0; pointer-events: none;
        }
        .tpl-token {
          display: inline-flex; align-items: center;
          padding: 1px 6px; margin: 0 1px; border-radius: 5px;
          font-family: var(--font-mono);
          font-size: 0.85em; font-weight: 600;
          border: 1px solid transparent;
          cursor: pointer;
          white-space: nowrap;
          vertical-align: baseline;
        }
        .tpl-token-source     { color: var(--color-token-source);      background: color-mix(in oklab, var(--color-token-source) 14%, transparent);      border-color: color-mix(in oklab, var(--color-token-source) 35%, transparent); }
        .tpl-token-prompt     { color: var(--color-token-prompt);      background: color-mix(in oklab, var(--color-token-prompt) 14%, transparent);      border-color: color-mix(in oklab, var(--color-token-prompt) 35%, transparent); }
        .tpl-token-conditional{ color: var(--color-token-conditional); background: color-mix(in oklab, var(--color-token-conditional) 14%, transparent); border-color: color-mix(in oklab, var(--color-token-conditional) 35%, transparent); }
        .tpl-token-repeat     { color: var(--color-token-repeat);      background: color-mix(in oklab, var(--color-token-repeat) 14%, transparent);      border-color: color-mix(in oklab, var(--color-token-repeat) 35%, transparent); }
        .tpl-token.ProseMirror-selectednode { outline: 2px solid var(--color-ring); outline-offset: 1px; }
      `}</style>
    </div>
  );
}

/* ------------------------- Toolbar ------------------------- */

function Toolbar({ editor, onInsertToken }: {
  editor: Editor;
  onInsertToken: (t: "source" | "prompt" | "conditional" | "repeat") => void;
}) {
  const btn = (opts: { onClick: () => void; active?: boolean; disabled?: boolean; icon: React.ReactNode; label: string }) => (
    <button
      onMouseDown={(e) => e.preventDefault()}
      onClick={opts.onClick}
      disabled={opts.disabled}
      title={opts.label}
      aria-label={opts.label}
      className={cn(
        "h-8 w-8 rounded-md flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors disabled:opacity-40 disabled:hover:bg-transparent",
        opts.active && "bg-brand/15 text-brand hover:bg-brand/20 hover:text-brand",
      )}
    >
      {opts.icon}
    </button>
  );
  const tokenBtn = (t: "source" | "prompt" | "conditional" | "repeat", Icon: any, label: string) => (
    <button
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => onInsertToken(t)}
      className="h-8 px-2.5 rounded-md inline-flex items-center gap-1.5 text-xs font-semibold border transition-colors hover:opacity-90"
      style={{
        color: `var(${TOKEN_COLORS[t].var})`,
        background: `color-mix(in oklab, var(${TOKEN_COLORS[t].var}) 12%, transparent)`,
        borderColor: `color-mix(in oklab, var(${TOKEN_COLORS[t].var}) 40%, transparent)`,
      }}
      title={`Insert ${label}`}
    >
      <Icon className="h-3.5 w-3.5" />
      {label}
    </button>
  );
  const sep = <div className="w-px h-5 bg-border mx-1" />;

  return (
    <div className="border-b border-border bg-surface/70 backdrop-blur px-4 py-1.5 flex items-center flex-wrap gap-1">
      {btn({ onClick: () => editor.chain().focus().undo().run(), disabled: !editor.can().undo(), icon: <Undo2 className="h-4 w-4" />, label: "Undo" })}
      {btn({ onClick: () => editor.chain().focus().redo().run(), disabled: !editor.can().redo(), icon: <Redo2 className="h-4 w-4" />, label: "Redo" })}
      {sep}
      {btn({ onClick: () => editor.chain().focus().setParagraph().run(), active: editor.isActive("paragraph"), icon: <Type className="h-4 w-4" />, label: "Paragraph" })}
      {btn({ onClick: () => editor.chain().focus().toggleHeading({ level: 1 }).run(), active: editor.isActive("heading", { level: 1 }), icon: <Heading1 className="h-4 w-4" />, label: "H1" })}
      {btn({ onClick: () => editor.chain().focus().toggleHeading({ level: 2 }).run(), active: editor.isActive("heading", { level: 2 }), icon: <Heading2 className="h-4 w-4" />, label: "H2" })}
      {btn({ onClick: () => editor.chain().focus().toggleHeading({ level: 3 }).run(), active: editor.isActive("heading", { level: 3 }), icon: <Heading3 className="h-4 w-4" />, label: "H3" })}
      {sep}
      {btn({ onClick: () => editor.chain().focus().toggleBold().run(), active: editor.isActive("bold"), icon: <Bold className="h-4 w-4" />, label: "Bold" })}
      {btn({ onClick: () => editor.chain().focus().toggleItalic().run(), active: editor.isActive("italic"), icon: <Italic className="h-4 w-4" />, label: "Italic" })}
      {btn({ onClick: () => editor.chain().focus().toggleUnderline().run(), active: editor.isActive("underline"), icon: <UnderlineIcon className="h-4 w-4" />, label: "Underline" })}
      {sep}
      {btn({ onClick: () => editor.chain().focus().toggleBulletList().run(), active: editor.isActive("bulletList"), icon: <List className="h-4 w-4" />, label: "Bullets" })}
      {btn({ onClick: () => editor.chain().focus().toggleOrderedList().run(), active: editor.isActive("orderedList"), icon: <ListOrdered className="h-4 w-4" />, label: "Numbered" })}
      {btn({ onClick: () => editor.chain().focus().toggleBlockquote().run(), active: editor.isActive("blockquote"), icon: <Quote className="h-4 w-4" />, label: "Quote" })}
      {btn({ onClick: () => editor.chain().focus().setHorizontalRule().run(), icon: <Minus className="h-4 w-4" />, label: "Divider" })}
      {sep}
      {tokenBtn("source", Braces, "Source")}
      {tokenBtn("prompt", Sparkles, "Prompt")}
      {tokenBtn("conditional", GitBranch, "If")}
      {tokenBtn("repeat", Repeat, "Repeat")}
    </div>
  );
}

/* ------------------------- Inspector ------------------------- */

function Inspector({
  selection, onChange, onDelete,
}: {
  selection: null | { type: "source" | "prompt" | "conditional" | "repeat"; attrs: Record<string, any>; pos: number };
  onChange: (patch: Record<string, any>) => void;
  onDelete: () => void;
}) {
  return (
    <aside className="border-l border-border bg-surface/40 p-5 overflow-auto hidden lg:block">
      <h3 className="font-semibold mb-1">Inspector</h3>
      <p className="text-xs text-muted-foreground mb-4">
        Click a colored token in the canvas to edit it.
      </p>

      {!selection && (
        <div className="rounded-lg border border-dashed border-border p-4 text-sm text-muted-foreground">
          No token selected.
        </div>
      )}

      {selection && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <span
              className="inline-flex items-center gap-1.5 text-xs font-semibold px-2 py-1 rounded"
              style={{
                color: `var(${TOKEN_COLORS[selection.type].var})`,
                background: `color-mix(in oklab, var(${TOKEN_COLORS[selection.type].var}) 14%, transparent)`,
              }}
            >
              {TOKEN_COLORS[selection.type].glyph} {TOKEN_COLORS[selection.type].label}
            </span>
            <button
              onClick={onDelete}
              className="text-xs text-destructive hover:underline inline-flex items-center gap-1"
            >
              <Trash2 className="h-3 w-3" /> Delete
            </button>
          </div>

          {selection.type === "source" && (
            <>
              <div className="space-y-1.5">
                <Label>Source field</Label>
                <Select
                  value={selection.attrs.field || ""}
                  onValueChange={(v) => onChange({ field: v })}
                >
                  <SelectTrigger><SelectValue placeholder="Pick a field" /></SelectTrigger>
                  <SelectContent>
                    {DEFAULT_SOURCE_FIELDS.map((f) => (
                      <SelectItem key={f} value={f}>{f}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>Fallback if empty</Label>
                <Input
                  value={selection.attrs.fallback || ""}
                  onChange={(e) => onChange({ fallback: e.target.value })}
                  placeholder="—"
                />
              </div>
            </>
          )}

          {selection.type === "prompt" && (
            <div className="space-y-1.5">
              <Label>LLM prompt</Label>
              <Textarea
                rows={5}
                value={selection.attrs.prompt || ""}
                onChange={(e) => onChange({ prompt: e.target.value })}
                placeholder="Write a warm two-sentence welcome…"
              />
              <p className="text-[11px] text-muted-foreground">
                Replaced at generate-time by the AI model.
              </p>
            </div>
          )}

          {selection.type === "conditional" && (
            <>
              <div className="space-y-1.5">
                <Label>Condition</Label>
                <Input
                  value={selection.attrs.condition || ""}
                  onChange={(e) => onChange({ condition: e.target.value })}
                  placeholder="region == 'EU'"
                />
              </div>
              <div className="space-y-1.5">
                <Label>Body when true</Label>
                <Textarea
                  rows={4}
                  value={selection.attrs.body || ""}
                  onChange={(e) => onChange({ body: e.target.value })}
                />
              </div>
            </>
          )}

          {selection.type === "repeat" && (
            <>
              <div className="grid grid-cols-2 gap-2">
                <div className="space-y-1.5">
                  <Label>Item var</Label>
                  <Input
                    value={selection.attrs.variable || ""}
                    onChange={(e) => onChange({ variable: e.target.value })}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label>Collection</Label>
                  <Input
                    value={selection.attrs.collection || ""}
                    onChange={(e) => onChange({ collection: e.target.value })}
                  />
                </div>
              </div>
              <div className="space-y-1.5">
                <Label>Row template</Label>
                <Textarea
                  rows={3}
                  value={selection.attrs.body || ""}
                  onChange={(e) => onChange({ body: e.target.value })}
                  placeholder="• {item}"
                />
              </div>
            </>
          )}
        </div>
      )}
    </aside>
  );
}

/* ------------------------- Default + helpers ------------------------- */

export const DEFAULT_TEMPLATE_HTML = `
<h1>Offer Letter</h1>
<p>Dear <span data-token="source" field="full_name"></span>,</p>
<p>We are pleased to offer you the position of <span data-token="source" field="role"></span> at our <span data-token="source" field="department"></span> team, starting <span data-token="source" field="start_date"></span>.</p>
<p>Your annual compensation will be <span data-token="source" field="salary"></span>.</p>
<p><span data-token="prompt" prompt="Write a warm 2-sentence welcome paragraph tailored to the role and department."></span></p>
<h2>Benefits</h2>
<p><span data-token="repeat" variable="benefit" collection="benefits" body="• {benefit}"></span></p>
<p><span data-token="conditional" condition="region == 'EU'" body="Your employment is subject to GDPR data-processing terms outlined in the appendix."></span></p>
<p>Welcome aboard,<br/>The People Team</p>
`;

/**
 * Convert plain text or basic HTML into template HTML, auto-detecting common
 * placeholder patterns: {field}, [FIELD], <<merge>>, [AI: prompt].
 */
export function autoDetectTokens(input: string): string {
  // AI markers first so they don't get eaten by generic bracket rule
  let s = input.replace(
    /\[AI:\s*([^\]]+)\]/gi,
    (_m, p) => `<span data-token="prompt" prompt="${escapeAttr(p.trim())}"></span>`,
  );
  s = s.replace(
    /\{\{?\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}?\}/g,
    (_m, f) => `<span data-token="source" field="${escapeAttr(f)}"></span>`,
  );
  s = s.replace(
    /<<\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*>>/g,
    (_m, f) => `<span data-token="source" field="${escapeAttr(f)}"></span>`,
  );
  s = s.replace(
    /\[([A-Z][A-Z0-9_ ]{2,})\]/g,
    (_m, f) => `<span data-token="source" field="${escapeAttr(f.toLowerCase().trim().replace(/\s+/g, "_"))}"></span>`,
  );
  // If input has no HTML tags, wrap paragraphs
  if (!/<[a-z][^>]*>/i.test(input)) {
    s = s.split(/\n\s*\n/).map((p) => `<p>${p.replace(/\n/g, "<br/>")}</p>`).join("");
  }
  return s;
}

function escapeAttr(v: string) {
  return v.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}
