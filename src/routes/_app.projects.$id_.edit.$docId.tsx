import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { useStore } from "@/lib/store";
import { api } from "@/lib/api";
import { TextDocumentEditor } from "@/components/text-document-editor";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { EditorContent, useEditor, type Editor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Underline from "@tiptap/extension-underline";
import Placeholder from "@tiptap/extension-placeholder";
import {
  Bold,
  Italic,
  Underline as UnderlineIcon,
  Strikethrough,
  Heading1,
  Heading2,
  Heading3,
  List,
  ListOrdered,
  Quote,
  Code2,
  Minus,
  Undo2,
  Redo2,
  ChevronRight,
  CheckCircle2,
  ArrowLeft,
  Save,
  Type,
  AlertTriangle,
  FileText,
  Download,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ReviewBar } from "@/components/review-bar";

export const Route = createFileRoute("/_app/projects/$id_/edit/$docId")({
  head: ({ params }) => ({
    meta: [
      { title: `Edit document — DocuMind AI` },
      { name: "description", content: "Rich document editor with block formatting and undo/redo for reviewing AI-generated drafts before approval." },
      { property: "og:title", content: "Edit generated draft — DocuMind AI" },
      { property: "og:description", content: "Refine AI-generated drafts with rich formatting before approval." },
    ],
  }),
  component: DocEditor,
});

// There is deliberately no placeholder document here.
//
// This file used to define a DEFAULT_HTML draft -- "Replace this section with
// the executive summary", "Point one from your source data" -- and fall back to
// it whenever a version had no html_content. Every document produced by the
// manifest path is exactly that case: it is a filled copy of the original .docx
// and carries no HTML at all. So opening a real generated letter showed
// invented content that looked like output, and one edit plus Save rebuilt the
// .docx from that placeholder, overwriting the letter in place.

function DocEditor() {
  const { id, docId } = Route.useParams();
  const project = useStore((s) => s.projects.find((p) => p.id === id));
  const loadProjectDetail = useStore((s) => s.loadProjectDetail);
  const doc = project?.generated.find((g) => g.id === docId);

  const [versionId, setVersionId] = useState<string | null>(null);
  const [initialHtml, setInitialHtml] = useState<string | null>(null);
  const [htmlEditable, setHtmlEditable] = useState(false);
  const [initialApproved, setInitialApproved] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!project) loadProjectDetail(id).catch((e: any) => setLoadError(e?.message ?? String(e)));
  }, [id, project]);

  useEffect(() => {
    api.getDocument(docId)
      .then((d) => api.getDocumentVersion(d.current_version_id).then((v) => {
        setVersionId(v.id);
        setInitialHtml(v.html_content ?? "");
        // The backend decides this from the renderer that produced the file.
        // Inferring it here from "does it have HTML?" is what put a rich-text
        // editor in front of template-rendered letters: they carry an HTML
        // preview, so they looked editable, and saving rebuilt the .docx from
        // scratch and lost the layout.
        setHtmlEditable(v.html_editable === true);
        setInitialApproved(v.status === "approved" || v.status === "final");
      }))
      .catch((e: any) => setLoadError(e?.message ?? String(e)));
  }, [docId]);

  if (loadError) {
    return (
      <div className="p-8 max-w-lg mx-auto text-center space-y-3">
        <p className="text-muted-foreground">{loadError}</p>
        <Link to="/dashboard" className="text-brand hover:underline">Back to dashboard</Link>
      </div>
    );
  }
  if (!project || !doc || versionId === null || initialHtml === null) {
    return <div className="p-8 text-muted-foreground">Loading document…</div>;
  }

  // Not HTML all the way down: the document *is* the .docx, and its layout came
  // from a Word template. Offering a rich-text editor over it would show content
  // that is not quite what is in the file, and saving would destroy the file.
  if (!htmlEditable) {
    return <TemplateGeneratedDocument projectId={id} versionId={versionId} filename={doc.filename} />;
  }

  return (
    <DocEditorInner
      key={versionId}
      projectId={id}
      projectName={project.name}
      filename={doc.filename}
      versionId={versionId}
      initialHtml={initialHtml}
      initialApproved={initialApproved}
    />
  );
}

function TemplateGeneratedDocument({ projectId, versionId, filename }: { projectId: string; versionId: string; filename: string }) {
  const [downloading, setDownloading] = useState(false);
  const [currentVersion, setCurrentVersion] = useState(versionId);
  // Bumped after an edit so the review bar reloads against the version that was
  // just minted rather than the one it replaced.
  const [stamp, setStamp] = useState(0);

  async function download() {
    setDownloading(true);
    try {
      const url = await api.authedDownloadUrl(currentVersion);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="p-6 lg:p-8 max-w-[1400px] mx-auto space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Link to="/projects/$id" params={{ id: projectId }} className="text-sm text-muted-foreground hover:text-foreground">
          ← Back to project
        </Link>
        <Button onClick={download} disabled={downloading} variant="outline">
          <Download className="h-4 w-4" /> {downloading ? "Preparing…" : "Download .docx"}
        </Button>
      </div>

      {/* Every letter the manifest pipeline produces opens here, and until now
          this branch had no approve control and no way to object -- both lived
          only on the HTML branch these documents never reach. */}
      <ReviewBar key={`${currentVersion}:${stamp}`} versionId={currentVersion} />

      {/* Says what this editor is FOR, not only what it refuses. The previous
          version of this screen offered nothing but a download and a warning,
          which was honest about the constraint and useless to a reviewer who
          needed to fix one sentence. */}
      <div className="flex items-start gap-2 rounded-lg border border-brand/40 bg-brand/5 px-3 py-2 text-sm">
        <AlertTriangle className="h-4 w-4 mt-0.5 text-brand shrink-0" />
        <span>
          This letter's layout comes from your Word template, so it is edited by its words rather
          than as HTML. Text changes are written back into the same runs and everything else — the
          letterhead, tables, headers and numbering — is left exactly as the template made it.
          To restructure the document, download it and open it in Word.
        </span>
      </div>

      <TextDocumentEditor
        versionId={currentVersion}
        filename={filename}
        onSaved={(newVersionId) => { setCurrentVersion(newVersionId); setStamp((n) => n + 1); }}
      />
    </div>
  );
}

function DocEditorInner({
  projectId, projectName, filename, versionId, initialHtml, initialApproved,
}: {
  projectId: string; projectName: string; filename: string; versionId: string;
  initialHtml: string; initialApproved: boolean;
}) {
  const navigate = useNavigate();
  const id = projectId;
  const [dirty, setDirty] = useState(false);
  const [approved, setApproved] = useState(initialApproved);

  const editor = useEditor({
    extensions: [
      StarterKit,
      Underline,
      Placeholder.configure({ placeholder: "Start writing…" }),
    ],
    content: initialHtml,
    editorProps: {
      attributes: {
        class:
          "prose-editor min-h-[60vh] focus:outline-none max-w-none text-foreground",
      },
    },
    onUpdate: () => setDirty(true),
  });

  useEffect(() => {
    return () => editor?.destroy();
  }, [editor]);

  if (!editor) return null;

  const save = () => {
    api.saveDocumentVersion(versionId, editor.getHTML())
      .then(() => {
        setDirty(false);
        toast.success("Draft saved");
      })
      .catch((e: any) => toast.error("Could not save", { description: e?.message ?? String(e) }));
  };

  return (
    <div className="flex flex-col h-full">
      {/* Top bar */}
      <div className="border-b border-border bg-surface/50 backdrop-blur px-6 py-3 flex items-center gap-4">
        <button
          onClick={() => navigate({ to: "/projects/$id", params: { id } })}
          className="h-8 w-8 rounded-lg border border-border hover:bg-accent flex items-center justify-center"
          aria-label="Back to project"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <div className="text-sm text-muted-foreground flex items-center gap-2 min-w-0">
          <Link to="/dashboard" className="hover:text-foreground">Projects</Link>
          <ChevronRight className="h-3.5 w-3.5 shrink-0" />
          <Link to="/projects/$id" params={{ id }} className="hover:text-foreground truncate">{projectName}</Link>
          <ChevronRight className="h-3.5 w-3.5 shrink-0" />
          <span className="text-foreground truncate">{filename}</span>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {dirty
            ? <span className="text-xs text-warning">Unsaved changes</span>
            : <span className="text-xs text-muted-foreground">All changes saved</span>}
          <Button variant="outline" size="sm" onClick={save} disabled={!dirty}>
            <Save className="h-4 w-4 mr-1.5" /> Save
          </Button>
        </div>
      </div>

      {/* The same component the template branch renders. Two copies of this bar
          is how one branch ended up with an approve button and the other with
          none. `beforeApprove` flushes the editor first, so approving is never
          a signature on text that is only in the browser. */}
      <div className="border-b border-border px-6 py-3">
        <ReviewBar
          versionId={versionId}
          onChanged={(status) => setApproved(status === "approved" || status === "final")}
          beforeApprove={async () => {
            await api.saveDocumentVersion(versionId, editor.getHTML());
            setDirty(false);
          }}
        />
      </div>

      {/* Editing rebuilds the .docx from this HTML, which discards the original
          template's letterhead, tables and headers. Say so before they lose it. */}
      <div className="border-b border-border bg-warning/10 px-6 py-2 text-xs text-warning flex items-center gap-2">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
        Saving here rebuilds the .docx from this editor's content — the original template's letterhead,
        tables and headers won't survive. Download first if you need the original layout.
      </div>

      {/* Toolbar */}
      <Toolbar editor={editor} />

      {/* Editor canvas */}
      <div className="flex-1 overflow-auto bg-background">
        <div className="max-w-3xl mx-auto my-8 rounded-xl border border-border bg-surface shadow-sm">
          <div className="px-10 py-12">
            <EditorContent editor={editor} />
          </div>
        </div>
      </div>

      {/* Local editor styles */}
      <style>{`
        .prose-editor h1 { font-size: 2rem; font-weight: 700; line-height: 1.2; margin: 1rem 0 0.75rem; letter-spacing: -0.02em; }
        .prose-editor h2 { font-size: 1.5rem; font-weight: 600; line-height: 1.3; margin: 1.25rem 0 0.5rem; letter-spacing: -0.01em; }
        .prose-editor h3 { font-size: 1.2rem; font-weight: 600; margin: 1rem 0 0.4rem; }
        .prose-editor p  { line-height: 1.7; margin: 0.5rem 0; }
        .prose-editor ul, .prose-editor ol { padding-left: 1.4rem; margin: 0.5rem 0; }
        .prose-editor ul { list-style: disc; }
        .prose-editor ol { list-style: decimal; }
        .prose-editor li { margin: 0.2rem 0; line-height: 1.6; }
        .prose-editor blockquote { border-left: 3px solid var(--color-brand); padding: 0.25rem 0 0.25rem 1rem; color: var(--color-muted-foreground); margin: 0.75rem 0; font-style: italic; }
        .prose-editor pre { background: var(--color-surface-elevated); border: 1px solid var(--color-border); border-radius: 0.5rem; padding: 0.75rem 1rem; font-family: var(--font-mono); font-size: 0.85rem; overflow-x: auto; margin: 0.75rem 0; }
        .prose-editor code { background: var(--color-surface-elevated); padding: 0.1rem 0.35rem; border-radius: 4px; font-family: var(--font-mono); font-size: 0.85em; }
        .prose-editor pre code { background: transparent; padding: 0; }
        .prose-editor hr { border: none; border-top: 1px solid var(--color-border); margin: 1.25rem 0; }
        .prose-editor strong { font-weight: 700; }
        .prose-editor em { font-style: italic; }
        .prose-editor u { text-decoration: underline; }
        .prose-editor s { text-decoration: line-through; }
        .prose-editor p.is-editor-empty:first-child::before {
          content: attr(data-placeholder);
          color: var(--color-muted-foreground);
          float: left;
          height: 0;
          pointer-events: none;
        }
      `}</style>
    </div>
  );
}

function Toolbar({ editor }: { editor: Editor }) {
  const btn = (opts: {
    onClick: () => void;
    active?: boolean;
    disabled?: boolean;
    icon: React.ReactNode;
    label: string;
  }) => (
    <button
      onMouseDown={(e) => e.preventDefault()}
      onClick={opts.onClick}
      disabled={opts.disabled}
      title={opts.label}
      aria-label={opts.label}
      aria-pressed={opts.active}
      className={cn(
        "h-8 w-8 rounded-md flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors disabled:opacity-40 disabled:hover:bg-transparent",
        opts.active && "bg-brand/15 text-brand hover:bg-brand/20 hover:text-brand",
      )}
    >
      {opts.icon}
    </button>
  );

  const sep = <div className="w-px h-5 bg-border mx-1" />;

  return (
    <div className="sticky top-0 z-10 border-b border-border bg-surface/80 backdrop-blur px-4 py-1.5 flex items-center flex-wrap gap-0.5">
      {btn({
        onClick: () => editor.chain().focus().undo().run(),
        disabled: !editor.can().undo(),
        icon: <Undo2 className="h-4 w-4" />,
        label: "Undo (⌘Z)",
      })}
      {btn({
        onClick: () => editor.chain().focus().redo().run(),
        disabled: !editor.can().redo(),
        icon: <Redo2 className="h-4 w-4" />,
        label: "Redo (⌘⇧Z)",
      })}
      {sep}
      {btn({
        onClick: () => editor.chain().focus().setParagraph().run(),
        active: editor.isActive("paragraph"),
        icon: <Type className="h-4 w-4" />,
        label: "Paragraph",
      })}
      {btn({
        onClick: () => editor.chain().focus().toggleHeading({ level: 1 }).run(),
        active: editor.isActive("heading", { level: 1 }),
        icon: <Heading1 className="h-4 w-4" />,
        label: "Heading 1",
      })}
      {btn({
        onClick: () => editor.chain().focus().toggleHeading({ level: 2 }).run(),
        active: editor.isActive("heading", { level: 2 }),
        icon: <Heading2 className="h-4 w-4" />,
        label: "Heading 2",
      })}
      {btn({
        onClick: () => editor.chain().focus().toggleHeading({ level: 3 }).run(),
        active: editor.isActive("heading", { level: 3 }),
        icon: <Heading3 className="h-4 w-4" />,
        label: "Heading 3",
      })}
      {sep}
      {btn({
        onClick: () => editor.chain().focus().toggleBold().run(),
        active: editor.isActive("bold"),
        icon: <Bold className="h-4 w-4" />,
        label: "Bold (⌘B)",
      })}
      {btn({
        onClick: () => editor.chain().focus().toggleItalic().run(),
        active: editor.isActive("italic"),
        icon: <Italic className="h-4 w-4" />,
        label: "Italic (⌘I)",
      })}
      {btn({
        onClick: () => editor.chain().focus().toggleUnderline().run(),
        active: editor.isActive("underline"),
        icon: <UnderlineIcon className="h-4 w-4" />,
        label: "Underline (⌘U)",
      })}
      {btn({
        onClick: () => editor.chain().focus().toggleStrike().run(),
        active: editor.isActive("strike"),
        icon: <Strikethrough className="h-4 w-4" />,
        label: "Strikethrough",
      })}
      {sep}
      {btn({
        onClick: () => editor.chain().focus().toggleBulletList().run(),
        active: editor.isActive("bulletList"),
        icon: <List className="h-4 w-4" />,
        label: "Bullet list",
      })}
      {btn({
        onClick: () => editor.chain().focus().toggleOrderedList().run(),
        active: editor.isActive("orderedList"),
        icon: <ListOrdered className="h-4 w-4" />,
        label: "Numbered list",
      })}
      {btn({
        onClick: () => editor.chain().focus().toggleBlockquote().run(),
        active: editor.isActive("blockquote"),
        icon: <Quote className="h-4 w-4" />,
        label: "Quote",
      })}
      {btn({
        onClick: () => editor.chain().focus().toggleCodeBlock().run(),
        active: editor.isActive("codeBlock"),
        icon: <Code2 className="h-4 w-4" />,
        label: "Code block",
      })}
      {btn({
        onClick: () => editor.chain().focus().setHorizontalRule().run(),
        icon: <Minus className="h-4 w-4" />,
        label: "Divider",
      })}
    </div>
  );
}
