/**
 * Start a template from nothing.
 *
 * A kit is a document, not a form. The old wizard's presets were lists of field
 * names used to bias a regex over text somebody had already written; these are
 * whole templates -- real prose with real placeholders in it -- because the
 * letter is the thing and the fields are the holes in it.
 *
 * What comes out is the same kind of object as a template read from a customer's
 * file: the placeholders are written in the same angle brackets, in the same
 * blue the pre-scanner classifies as a placeholder. So there is no
 * "from-scratch" branch anywhere downstream.
 */

import { useEffect, useState } from "react";
import { FileText, Loader2, Plus } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";

type Kit = {
  id: string; name: string; description: string; field_count: number; paragraph_count: number;
};

export function BlueprintKitDialog({
  open, onOpenChange, onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (blueprintId: string) => void;
}) {
  const [kits, setKits] = useState<Kit[]>([]);
  const [kit, setKit] = useState("blank");
  const [name, setName] = useState("");
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const [projectId, setProjectId] = useState("");
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    if (!open) return;
    api.blueprintKits().then((r) => setKits(r.items)).catch(() => setKits([]));
    api.listProjects("", { limit: 100 })
      .then((r) => setProjects(r.items.map((p: any) => ({ id: p.id, name: p.name }))))
      .catch(() => setProjects([]));
  }, [open]);

  const create = async () => {
    setCreating(true);
    try {
      const blueprint = await api.createBlueprint({
        name: name.trim() || kits.find((k) => k.id === kit)?.name || "Untitled template",
        kit,
        project_id: projectId || undefined,
      });
      onOpenChange(false);
      onCreated(blueprint.id);
    } catch (e: any) {
      toast.error("Could not create this template", { description: e?.message ?? String(e) });
    } finally {
      setCreating(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Plus className="h-4 w-4 text-primary" /> Start a template
          </DialogTitle>
          <DialogDescription>
            Each of these is a document you can edit, not a list of fields. Placeholders are written
            the way a Word template writes them, so what you build is read back exactly like a
            template somebody sent you.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-2 py-2 sm:grid-cols-2">
          {kits.map((k) => (
            <button
              key={k.id}
              onClick={() => setKit(k.id)}
              className={cn(
                "rounded-lg border p-3 text-left transition-colors",
                kit === k.id ? "border-primary bg-primary/5" : "border-border hover:bg-accent/40",
              )}
            >
              <div className="flex items-center gap-2 font-medium">
                <FileText className="h-3.5 w-3.5 text-primary" /> {k.name}
              </div>
              <p className="mt-1 text-xs text-muted-foreground">{k.description}</p>
              <p className="mt-1.5 text-[11px] text-muted-foreground">
                {k.paragraph_count} paragraphs · {k.field_count} placeholders
              </p>
            </button>
          ))}
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>Name</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)}
                   placeholder={kits.find((k) => k.id === kit)?.name ?? "Untitled template"} />
          </div>
          <div className="space-y-1.5">
            <Label>Project</Label>
            <Select value={projectId} onValueChange={setProjectId}>
              <SelectTrigger><SelectValue placeholder="Choose a project" /></SelectTrigger>
              <SelectContent>
                {projects.map((p) => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}
              </SelectContent>
            </Select>
            <p className="text-[11px] text-muted-foreground">
              Needed before the template can be written out — that is where its file and its source
              data live.
            </p>
          </div>
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={creating}>Cancel</Button>
          <Button onClick={create} disabled={creating} className="gap-2">
            {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
