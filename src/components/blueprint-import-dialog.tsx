/**
 * Read a legacy .docx into an editable template.
 *
 * This replaces the client-side conversion wizard, and the reason is worth
 * stating because the old one looked like it did more. It ran
 * `mammoth.extractRawText()` in the browser and then twelve regexes over the
 * result -- but `extractRawText` throws away run colour, MERGEFIELDs and table
 * structure, which are the three things the backend's pre-scanner exists to
 * read. So the wizard could only ever guess at what a template meant, and what
 * it produced was HTML that no manifest could be compiled from.
 *
 * Here the file goes to the server, where the same pre-scan and compile every
 * uploaded template gets is run against it, and what comes back is a document
 * with its placeholders, conditions and author instructions already identified.
 */

import { useEffect, useState } from "react";
import { FileText, Loader2, Upload, Wand2 } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { CompileProgressList, useCompileProgress } from "@/components/compile-progress";
import { cn } from "@/lib/utils";

type Project = { id: string; name: string };
type Template = { id: string; name: string; status: string; current_version_id: string | null };

export function BlueprintImportDialog({
  open, onOpenChange, onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (blueprintId: string) => void;
}) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [templates, setTemplates] = useState<Template[]>([]);
  const [templateId, setTemplateId] = useState("");
  const [uploading, setUploading] = useState(false);
  const [reading, setReading] = useState(false);
  const { stages, failed, newToken } = useCompileProgress(reading);

  useEffect(() => {
    if (!open) return;
    api.listProjects("", { limit: 100 })
      .then((r) => setProjects(r.items.map((p: any) => ({ id: p.id, name: p.name }))))
      .catch((e: any) => toast.error("Could not load projects", { description: e?.message }));
  }, [open]);

  useEffect(() => {
    if (!projectId) return setTemplates([]);
    setTemplateId("");
    api.listTemplates(projectId)
      .then((r) => setTemplates(r.items))
      .catch((e: any) => toast.error("Could not load templates", { description: e?.message }));
  }, [projectId]);

  const upload = async (file: File) => {
    if (!projectId) return;
    setUploading(true);
    try {
      const created = await api.uploadTemplate(projectId, file, file.name);
      const refreshed = await api.listTemplates(projectId);
      setTemplates(refreshed.items);
      setTemplateId(created.id);
      toast.success("Uploaded", { description: file.name });
    } catch (e: any) {
      toast.error("Could not upload", { description: e?.message ?? String(e) });
    } finally {
      setUploading(false);
    }
  };

  const read = async () => {
    if (!templateId) return;
    setReading(true);
    const progressToken = newToken();
    try {
      const blueprint = await api.blueprintFromTemplate({
        template_file_id: templateId,
        progress_token: progressToken,
      });
      toast.success("Template read", { description: blueprint.name });
      onOpenChange(false);
      onCreated(blueprint.id);
    } catch (e: any) {
      // The honest failure. Reading a template needs a model; a blueprint built
      // from an empty reading would look exactly like a good reading of a
      // template that asks for nothing, so the server refuses rather than
      // handing back something that looks fine and is not.
      toast.error(
        e?.code === "LLM_NOT_CONFIGURED" ? "No language model is configured" : "Could not read this template",
        { description: e?.message ?? String(e), duration: 8000 },
      );
    } finally {
      setReading(false);
    }
  };

  const chosen = templates.find((t) => t.id === templateId);

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!reading) onOpenChange(next); }}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Wand2 className="h-4 w-4 text-primary" /> Read a legacy template
          </DialogTitle>
          <DialogDescription>
            The document is pre-scanned and compiled on the server — colour-coded placeholders,
            author instructions and conditional sections are identified, and what comes back is a
            template you can edit.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="space-y-1.5">
            <Label>Project</Label>
            <Select value={projectId} onValueChange={setProjectId} disabled={reading}>
              <SelectTrigger><SelectValue placeholder="Choose a project" /></SelectTrigger>
              <SelectContent>
                {projects.map((p) => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}
              </SelectContent>
            </Select>
            <p className="text-[11px] text-muted-foreground">
              Templates are uploaded into a project, and the one you read stays there as the
              original this template can always be put back to.
            </p>
          </div>

          {projectId && (
            <div className="space-y-1.5">
              <Label>Template</Label>
              {templates.length > 0 && (
                <Select value={templateId} onValueChange={setTemplateId} disabled={reading}>
                  <SelectTrigger><SelectValue placeholder="Choose an uploaded template" /></SelectTrigger>
                  <SelectContent>
                    {templates.map((t) => (
                      <SelectItem key={t.id} value={t.id} disabled={!t.current_version_id}>
                        {t.name}{t.status === "failed" ? " — could not be parsed" : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
              <label
                className={cn(
                  "mt-2 flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-dashed",
                  "border-border px-4 py-6 text-sm text-muted-foreground transition-colors hover:bg-accent/40",
                  (uploading || reading) && "pointer-events-none opacity-60",
                )}
              >
                {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
                {uploading ? "Uploading…" : "or drop a .docx here to upload it"}
                <input
                  type="file"
                  accept=".docx,.dotx"
                  className="hidden"
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = ""; }}
                />
              </label>
            </div>
          )}

          {chosen && (
            <div className="flex items-start gap-2 rounded-lg border border-border bg-muted/40 p-3 text-xs">
              <FileText className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
              <span>
                <span className="font-medium">{chosen.name}</span> will be read and compiled. The
                original file is kept, so this template can always be put back to it.
              </span>
            </div>
          )}

          {reading && (
            <div className="rounded-lg border border-border p-3">
              <p className="mb-2 text-xs text-muted-foreground">
                Reading the template. The pre-scan is deterministic; the compile is the one step
                that uses a model.
              </p>
              <CompileProgressList stages={stages} failed={failed} />
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={reading}>Cancel</Button>
          <Button onClick={read} disabled={!templateId || reading} className="gap-2">
            {reading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wand2 className="h-4 w-4" />}
            {reading ? "Reading…" : "Read it"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
