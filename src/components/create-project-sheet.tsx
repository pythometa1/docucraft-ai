import { useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { toast } from "sonner";
import { useStore, FUNCTIONS, DOCUMENT_TYPES, REGIONS } from "@/lib/store";
import type { FunctionKey } from "@/lib/types";
import { ChevronDown } from "lucide-react";

export function CreateProjectSheet({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const navigate = useNavigate();
  const createProject = useStore((s) => s.createProject);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [region, setRegion] = useState("Europe");
  const [fn, setFn] = useState<FunctionKey | "">("");
  const [docType, setDocType] = useState("");
  const [language, setLanguage] = useState("English");
  const [advanced, setAdvanced] = useState(false);
  const [approval, setApproval] = useState(false);
  const [audit, setAudit] = useState(true);

  const docOptions = fn ? DOCUMENT_TYPES[fn] ?? [] : [];
  const canCreate = name && fn && docType;
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (!canCreate || submitting) return;
    setSubmitting(true);
    try {
      const id = await createProject({
        name,
        description,
        region,
        function: fn as FunctionKey,
        documentType: docType,
        language,
      });
      toast.success("Project created", { description: `"${name}" is ready to configure.` });
      onOpenChange(false);
      navigate({ to: "/projects/$id", params: { id } });
    } catch (e: any) {
      toast.error("Could not create project", { description: e?.message ?? String(e) });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-md bg-surface border-l border-border p-0 flex flex-col">
        <SheetHeader className="p-6 border-b border-border">
          <SheetTitle>Create new project</SheetTitle>
          <SheetDescription>Provide the details to create a project</SheetDescription>
        </SheetHeader>

        <div className="flex-1 overflow-y-auto p-6 space-y-5">
          <div className="space-y-2">
            <Label htmlFor="name">Project name *</Label>
            <Input id="name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Q3 Termination Letters" />
          </div>

          <div className="space-y-2">
            <Label htmlFor="desc">Description</Label>
            <Textarea id="desc" rows={3} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Optional details about this project" />
          </div>

          <div className="space-y-2">
            <Label>Region *</Label>
            <Select value={region} onValueChange={setRegion}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {REGIONS.map((r) => <SelectItem key={r} value={r}>{r}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label>Function *</Label>
            <Select value={fn} onValueChange={(v) => { setFn(v as FunctionKey); setDocType(""); }}>
              <SelectTrigger><SelectValue placeholder="Select function" /></SelectTrigger>
              <SelectContent>
                {FUNCTIONS.map((f) => <SelectItem key={f} value={f}>{f}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label>Document type *</Label>
            <Select value={docType} onValueChange={setDocType} disabled={!fn}>
              <SelectTrigger><SelectValue placeholder={fn ? "Select document type" : "Select function first"} /></SelectTrigger>
              <SelectContent>
                {docOptions.map((d) => <SelectItem key={d} value={d}>{d}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label>Language</Label>
            <Select value={language} onValueChange={setLanguage}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {["English", "Spanish", "French", "German", "Japanese", "Chinese"].map((l) => (
                  <SelectItem key={l} value={l}>{l}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="pt-2 border-t border-border">
            <button
              onClick={() => setAdvanced((v) => !v)}
              className="flex items-center gap-1.5 text-sm font-medium text-muted-foreground hover:text-foreground"
            >
              <ChevronDown className={`h-4 w-4 transition-transform ${advanced ? "rotate-180" : ""}`} />
              Advanced
            </button>
            {advanced && (
              <div className="mt-4 space-y-4">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-sm font-medium">Enable approval workflow</div>
                    <div className="text-xs text-muted-foreground">Route documents through reviewers.</div>
                  </div>
                  <Switch checked={approval} onCheckedChange={setApproval} />
                </div>
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-sm font-medium">Enable audit trail</div>
                    <div className="text-xs text-muted-foreground">Log every action for compliance.</div>
                  </div>
                  <Switch checked={audit} onCheckedChange={setAudit} />
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="p-6 border-t border-border flex items-center justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button
            onClick={submit}
            disabled={!canCreate || submitting}
            className="bg-gradient-brand text-white hover:opacity-90"
          >
            Create
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
