/**
 * The clinical service, living where the user already works: inside a project.
 *
 * There is no separate "Clinical" section in the portal. A project whose
 * function is Clinical renders this instead of the generic four-stage
 * pipeline -- the same screen answers "write the study report", "what have we
 * issued for this study", and "which studies do we run", scoped to the
 * project the person opened. The project's document type picks the service:
 * a CSR project authors study reports, an Informed Consent project authors
 * consent forms, each with its own prompt pack, fallback kit and numbering.
 *
 * Derived figures (row counts, per-column totals) shown while typing are a
 * floating-point preview; the server re-derives them in Decimal and its
 * answer is the one the document prints. The document number is never shown
 * before generation, because it does not exist until the transaction that
 * stores the document allocates it.
 */

import { useEffect, useMemo, useState } from "react";
import { Link } from "@tanstack/react-router";
import {
  ArrowLeft, ArrowRight, Ban, Check, Download, FileText, FlaskConical,
  Pencil, Plus, Sparkles, Stethoscope, Trash2, Wand2,
} from "lucide-react";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import type { Blueprint, ClinicalDocSummary, Study } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { ErrorBanner } from "@/components/error-banner";
import { plainly } from "@/components/processing-banner";
import { qaNoteLines } from "@/lib/friendly";
import { PolishedEmpty, StageSkeleton, TableSkeleton } from "@/components/skeletons";
import { SwapIn } from "@/components/motion";
import {
  CLINICAL_COMPUTED_PATTERN, CLINICAL_SERVER_COMPUTED_FIELD_IDS,
  FieldValueForm, tableRowSpec, typedFields,
  type ManifestFieldLike,
} from "@/components/field-value-form";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ shared */

/** The project's document type picks the authoring service and the request
 *  key the generate endpoint expects. Unknown types behave as a CSR rather
 *  than dead-ending -- the taxonomy can grow before this map does. */
const SERVICE_BY_DOC_TYPE: Record<string, { service: string; docTypeKey: string }> = {
  "Clinical Study Report": { service: "clinical_csr", docTypeKey: "csr" },
  "Protocol Amendment": { service: "clinical_protocol_amendment", docTypeKey: "protocol_amendment" },
  "Informed Consent": { service: "clinical_icf", docTypeKey: "icf" },
  "Investigator Brochure": { service: "clinical_ib", docTypeKey: "investigator_brochure" },
};

function serviceFor(documentType: string): { service: string; docTypeKey: string } {
  return SERVICE_BY_DOC_TYPE[documentType] ?? SERVICE_BY_DOC_TYPE["Clinical Study Report"];
}

/** Whether this studio actually serves a document type. The project page
 *  gates on this, so a NEW clinical document type added to the taxonomy gets
 *  the generic pipeline until it gets a real service here -- never a CSR
 *  prompt pack and a CSR-#### number wearing the wrong label. */
export function hasClinicalService(documentType: string): boolean {
  return documentType in SERVICE_BY_DOC_TYPE;
}

const CLINICAL_COMPUTED = {
  ids: CLINICAL_SERVER_COMPUTED_FIELD_IDS,
  pattern: CLINICAL_COMPUTED_PATTERN,
};

function shortDate(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleDateString();
}

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

async function saveBlob(url: string, filename: string) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function typeLabel(documentType: string): string {
  const labels: Record<string, string> = {
    csr: "Study report", protocol_amendment: "Amendment",
    icf: "Consent form", investigator_brochure: "Brochure",
  };
  return labels[documentType] ?? documentType;
}

const STATUS_TONE: Record<string, string> = {
  final: "bg-success/15 text-success border-success/30",
  draft: "bg-warning/15 text-warning border-warning/30",
  void: "bg-muted text-muted-foreground border-border line-through",
};

function StatusChip({ status }: { status: string }) {
  return (
    <span className={cn(
      "inline-flex rounded-full border px-2 py-0.5 text-xs font-medium capitalize",
      STATUS_TONE[status] ?? "bg-muted text-muted-foreground border-border",
    )}>
      {status}
    </span>
  );
}

/* ------------------------------------------------------------------ studio */

type StudioProject = { id: string; name: string; documentType: string };

export function ClinicalStudio({ project }: { project: StudioProject }) {
  const [tab, setTab] = useState<"create" | "documents" | "studies">("create");
  // Bumped when a generation lands, so the registry refetches on tab switch.
  const [generation, setGeneration] = useState(0);

  return (
    <div className="space-y-4">
      <div className="flex gap-1 border-b border-border">
        {([["create", "New document", Wand2],
           ["documents", "Documents", FileText],
           ["studies", "Studies", FlaskConical]] as const).map(([key, title, Icon]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={cn(
              "inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition-colors",
              tab === key
                ? "border-brand text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            <Icon className="h-4 w-4" /> {title}
          </button>
        ))}
      </div>

      <SwapIn k={tab}>
        {tab === "create" && (
          <ClinicalWizard
            projectId={project.id}
            documentType={project.documentType}
            onIssued={() => setGeneration((n) => n + 1)}
            onViewAll={() => setTab("documents")}
          />
        )}
        {tab === "documents" && <DocumentList projectId={project.id} refreshKey={generation} />}
        {tab === "studies" && <StudyBook />}
      </SwapIn>
    </div>
  );
}

/* ------------------------------------------------------------------ registry */

function DocumentList({ projectId, refreshKey }: { projectId: string; refreshKey: number }) {
  const [items, setItems] = useState<ClinicalDocSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = async () => {
    setError(null);
    try {
      const res = await api.listClinicalDocuments({ project_id: projectId });
      setItems(res.items);
    } catch (e: any) {
      setError(e?.message ?? String(e));
      setItems([]);
    }
  };

  useEffect(() => {
    let live = true;
    api.listClinicalDocuments({ project_id: projectId })
      .then((res) => { if (live) setItems(res.items); })
      .catch((e) => { if (live) { setError(e?.message ?? String(e)); setItems([]); } });
    return () => { live = false; };
  }, [projectId, refreshKey]);

  async function download(doc: ClinicalDocSummary, format: "docx" | "pdf") {
    if (!doc.document_version_id) {
      toast.error("This document has no stored file.");
      return;
    }
    setBusy(`${doc.id}:${format}`);
    try {
      const url = await api.downloadVersion(doc.document_version_id, format);
      await saveBlob(url, `${doc.number}.${format}`);
    } catch (e: any) {
      toast.error(format === "pdf" ? "PDF is not available" : "Download failed", {
        description: plainly(String(e?.message ?? e)),
      });
    } finally {
      setBusy(null);
    }
  }

  async function voidDocument(doc: ClinicalDocSummary) {
    setBusy(`${doc.id}:void`);
    try {
      await api.voidClinicalDocument(doc.id);
      toast.success(`${doc.number} voided. Its number is kept — a numbering with silent gaps is worse.`);
      await load();
    } catch (e: any) {
      toast.error("Could not void this document", { description: plainly(String(e?.message ?? e)) });
    } finally {
      setBusy(null);
    }
  }

  if (items === null) return <TableSkeleton rows={5} cols={6} />;
  if (error) return <ErrorBanner title="Documents could not be loaded" message="Try again in a moment." detail={error} />;
  if (items.length === 0) {
    return (
      <PolishedEmpty
        icon={<FileText className="h-8 w-8 text-muted-foreground" />}
        title="No documents in this project yet"
        subtitle="Use the New document tab — describe the study, let the AI draft the template, type the values."
      />
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border bg-muted/40 text-left text-muted-foreground">
            <th className="px-4 py-2.5 font-medium">Number</th>
            <th className="px-4 py-2.5 font-medium">Study</th>
            <th className="px-4 py-2.5 font-medium">Type</th>
            <th className="px-4 py-2.5 font-medium">Date</th>
            <th className="px-4 py-2.5 font-medium">Status</th>
            <th className="px-4 py-2.5 text-right font-medium">Actions</th>
          </tr>
        </thead>
        <tbody>
          {items.map((doc) => (
            <tr key={doc.id} className="border-b border-border/60 last:border-0 hover:bg-accent/40">
              <td className="px-4 py-2.5 font-mono text-xs font-medium text-foreground">{doc.number}</td>
              <td className="px-4 py-2.5">{doc.study?.protocol_number ?? doc.study?.title ?? "—"}</td>
              <td className="px-4 py-2.5 text-muted-foreground">{typeLabel(doc.document_type)}</td>
              <td className="px-4 py-2.5 text-muted-foreground">{shortDate(doc.document_date)}</td>
              <td className="px-4 py-2.5">
                <StatusChip status={doc.status} />
                {!doc.qa_passed && (
                  <span className="ml-1.5 text-xs text-ai-blocked" title="This document failed its generation checks.">Failed checks</span>
                )}
              </td>
              <td className="px-4 py-2.5">
                <div className="flex items-center justify-end gap-1">
                  <button
                    onClick={() => download(doc, "docx")}
                    disabled={busy !== null || !doc.document_version_id}
                    className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-accent disabled:opacity-40"
                    title="Download DOCX"
                  >
                    <Download className="h-4 w-4" />
                  </button>
                  <button
                    onClick={() => download(doc, "pdf")}
                    disabled={busy !== null || !doc.document_version_id}
                    className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-accent disabled:opacity-40"
                    title="Download PDF"
                  >
                    <FileText className="h-4 w-4" />
                  </button>
                  {doc.status !== "void" && (
                    <button
                      onClick={() => voidDocument(doc)}
                      disabled={busy !== null}
                      className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive disabled:opacity-40"
                      title="Void this document (its number is kept)"
                    >
                      <Ban className="h-4 w-4" />
                    </button>
                  )}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------------ studies */

const EMPTY_STUDY = {
  protocol_number: "", title: "", sponsor: "", phase: "", indication: "",
  principal_investigator: "", notes: "",
};

function StudyBook() {
  const [items, setItems] = useState<Study[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Study | "new" | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = async () => {
    setError(null);
    try {
      const res = await api.listStudies();
      setItems(res.items);
    } catch (e: any) {
      setError(e?.message ?? String(e));
      setItems([]);
    }
  };

  useEffect(() => {
    let live = true;
    api.listStudies()
      .then((res) => { if (live) setItems(res.items); })
      .catch((e) => { if (live) { setError(e?.message ?? String(e)); setItems([]); } });
    return () => { live = false; };
  }, []);

  async function remove(study: Study) {
    setBusy(study.id);
    try {
      await api.deleteStudy(study.id);
      toast.success(`${study.protocol_number} removed. Existing documents keep their snapshot.`);
      await load();
    } catch (e: any) {
      toast.error("Could not remove this study", { description: plainly(String(e?.message ?? e)) });
    } finally {
      setBusy(null);
    }
  }

  if (items === null) return <TableSkeleton rows={4} cols={4} />;
  if (error) return <ErrorBanner title="Studies could not be loaded" message="Try again in a moment." detail={error} />;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          The whole workspace's study book — saved once, referenced by every document, in any project.
        </p>
        <Button variant="outline" onClick={() => setEditing("new")}>
          <Plus className="mr-1.5 h-4 w-4" /> Add study
        </Button>
      </div>

      {items.length === 0 ? (
        <PolishedEmpty
          icon={<FlaskConical className="h-8 w-8 text-muted-foreground" />}
          title="No studies yet"
          subtitle="Save a study once — protocol number, sponsor, investigator — and every later document fills its identity in a click."
          action={<Button variant="outline" onClick={() => setEditing("new")}>Add your first study</Button>}
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/40 text-left text-muted-foreground">
                <th className="px-4 py-2.5 font-medium">Protocol</th>
                <th className="px-4 py-2.5 font-medium">Title</th>
                <th className="px-4 py-2.5 font-medium">Sponsor</th>
                <th className="px-4 py-2.5 font-medium">Phase</th>
                <th className="px-4 py-2.5 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((study) => (
                <tr key={study.id} className="border-b border-border/60 last:border-0 hover:bg-accent/40">
                  <td className="px-4 py-2.5 font-mono text-xs font-medium text-foreground">{study.protocol_number}</td>
                  <td className="px-4 py-2.5">{study.title ?? "—"}</td>
                  <td className="px-4 py-2.5 text-muted-foreground">{study.sponsor ?? "—"}</td>
                  <td className="px-4 py-2.5">{study.phase ?? "—"}</td>
                  <td className="px-4 py-2.5">
                    <div className="flex items-center justify-end gap-1">
                      <button
                        onClick={() => setEditing(study)}
                        className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-accent"
                        title="Edit"
                      >
                        <Pencil className="h-4 w-4" />
                      </button>
                      <button
                        onClick={() => remove(study)}
                        disabled={busy === study.id}
                        className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive disabled:opacity-40"
                        title="Remove"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <StudyDialog
        editing={editing}
        onClose={() => setEditing(null)}
        onSaved={async () => { setEditing(null); await load(); }}
      />
    </div>
  );
}

export function StudyDialog({ editing, onClose, onSaved }: {
  editing: Study | "new" | null;
  onClose: () => void;
  onSaved: (study: Study) => void | Promise<void>;
}) {
  const [form, setForm] = useState(EMPTY_STUDY);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (editing === "new") setForm(EMPTY_STUDY);
    else if (editing) {
      setForm({
        protocol_number: editing.protocol_number ?? "", title: editing.title ?? "",
        sponsor: editing.sponsor ?? "", phase: editing.phase ?? "",
        indication: editing.indication ?? "",
        principal_investigator: editing.principal_investigator ?? "",
        notes: editing.notes ?? "",
      });
    }
  }, [editing]);

  async function save() {
    if (!form.protocol_number.trim()) {
      toast.error("A study needs a protocol number.");
      return;
    }
    setSaving(true);
    const payload = {
      protocol_number: form.protocol_number.trim(),
      title: form.title.trim() || null,
      sponsor: form.sponsor.trim() || null,
      phase: form.phase.trim() || null,
      indication: form.indication.trim() || null,
      principal_investigator: form.principal_investigator.trim() || null,
      notes: form.notes.trim() || null,
    };
    try {
      const saved = editing === "new" || editing === null
        ? await api.createStudy(payload as any)
        : await api.updateStudy(editing.id, payload as any);
      await onSaved(saved);
    } catch (e: any) {
      toast.error("Could not save this study", { description: plainly(String(e?.message ?? e)) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={editing !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{editing === "new" ? "Add a study" : "Edit study"}</DialogTitle>
          <DialogDescription>
            Saved once, referenced by every document. Existing documents keep the identity they were issued with.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <Input placeholder="Protocol number" value={form.protocol_number}
                 onChange={(e) => setForm({ ...form, protocol_number: e.target.value })} />
          <Textarea placeholder="Study title" rows={2} value={form.title}
                    onChange={(e) => setForm({ ...form, title: e.target.value })} />
          <div className="grid grid-cols-2 gap-3">
            <Input placeholder="Sponsor" value={form.sponsor}
                   onChange={(e) => setForm({ ...form, sponsor: e.target.value })} />
            <Input placeholder="Phase (1, 2, 3…)" value={form.phase}
                   onChange={(e) => setForm({ ...form, phase: e.target.value })} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Input placeholder="Indication" value={form.indication}
                   onChange={(e) => setForm({ ...form, indication: e.target.value })} />
            <Input placeholder="Principal investigator" value={form.principal_investigator}
                   onChange={(e) => setForm({ ...form, principal_investigator: e.target.value })} />
          </div>
          <Textarea placeholder="Notes" rows={2} value={form.notes}
                    onChange={(e) => setForm({ ...form, notes: e.target.value })} />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>Cancel</Button>
          <Button onClick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ wizard */

const STEPS = ["Template", "Details", "Content", "Review"] as const;

type ManifestData = {
  id: string;
  fields: ManifestFieldLike[];
  blocks: Record<string, unknown>[];
};

function ClinicalWizard({ projectId, documentType, onIssued, onViewAll }: {
  projectId: string;
  documentType: string;
  onIssued: () => void;
  onViewAll: () => void;
}) {
  const [step, setStep] = useState(0);
  const { docTypeKey } = serviceFor(documentType);

  const [manifest, setManifest] = useState<ManifestData | null>(null);
  const [blueprintName, setBlueprintName] = useState<string | null>(null);

  const [studyId, setStudyId] = useState<string | null>(null);
  const [oneOff, setOneOff] = useState({ protocol_number: "", title: "", sponsor: "", principal_investigator: "" });
  const [studies, setStudies] = useState<Study[] | null>(null);
  const [addingStudy, setAddingStudy] = useState(false);
  const [documentDate, setDocumentDate] = useState(todayISO());
  const [versionLabel, setVersionLabel] = useState("1.0");
  const [docTitle, setDocTitle] = useState("");

  const [values, setValues] = useState<Record<string, unknown>>({});
  const [rows, setRows] = useState<Record<string, unknown>[]>([{}]);

  const [result, setResult] = useState<Awaited<ReturnType<typeof api.generateClinicalDocument>> | null>(null);

  useEffect(() => {
    let live = true;
    api.listStudies()
      .then((res) => { if (live) setStudies(res.items); })
      .catch(() => { if (live) setStudies([]); });
    return () => { live = false; };
  }, []);

  const spec = useMemo(() => tableRowSpec(manifest?.blocks), [manifest]);
  const scalarFields = useMemo(
    () => typedFields(manifest?.fields, spec, CLINICAL_COMPUTED),
    [manifest, spec],
  );

  const selectedStudy = studies?.find((s) => s.id === studyId) ?? null;

  // A display-only strip: the row count and, per numeric column, the sum --
  // shown only when every row parses, the same honesty rule the server's
  // Decimal derivation applies before a total may print.
  const rowPreview = useMemo(() => {
    const cleanRows = rows.filter((row) => Object.values(row).some((v) => v != null && String(v).trim() !== ""));
    const sums: { label: string; value: number }[] = [];
    for (const column of spec?.columns ?? []) {
      if (column.type !== "number" && column.type !== "currency") continue;
      let total = 0;
      let complete = cleanRows.length > 0;
      for (const row of cleanRows) {
        const cell = row[column.source_key];
        const n = Number(cell);
        if (cell == null || String(cell).trim() === "" || !Number.isFinite(n)) {
          complete = false;
          break;
        }
        total += n;
      }
      if (complete) {
        sums.push({
          value: total,
          label: column.source_key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
        });
      }
    }
    return { count: cleanRows.length, sums };
  }, [rows, spec]);

  async function generate() {
    if (!manifest) return;
    if (!studyId && !oneOff.protocol_number.trim()) {
      toast.error("A clinical document is about a study — pick one or type at least a protocol number.");
      setStep(1);
      return;
    }
    const cleanRows = rows.filter((row) => Object.values(row).some((v) => v != null && String(v).trim() !== ""));
    setResult(null);
    try {
      const generated = await api.generateClinicalDocument({
        manifest_id: manifest.id,
        document_type: docTypeKey,
        project_id: projectId,
        study_id: studyId ?? undefined,
        study: studyId ? undefined : {
          protocol_number: oneOff.protocol_number.trim() || undefined,
          title: oneOff.title.trim() || undefined,
          sponsor: oneOff.sponsor.trim() || undefined,
          principal_investigator: oneOff.principal_investigator.trim() || undefined,
        },
        rows: cleanRows,
        fields: values,
        title: docTitle.trim() || undefined,
        document_date: documentDate || undefined,
        version_label: versionLabel.trim() || undefined,
      });
      setResult(generated);
      setStep(4);
      onIssued();
    } catch (e: any) {
      toast.error("The document could not be generated", { description: plainly(String(e?.message ?? e)) });
    }
  }

  return (
    <div className="space-y-5">
      {step < 4 && (
        <ol className="flex items-center gap-2 text-sm">
          {STEPS.map((title, index) => (
            <li key={title} className="flex items-center gap-2">
              <span className={cn(
                "flex h-6 w-6 items-center justify-center rounded-full border text-xs font-semibold",
                index < step && "border-success bg-success/15 text-success",
                index === step && "border-brand bg-brand/15 text-brand",
                index > step && "border-border text-muted-foreground",
              )}>
                {index < step ? <Check className="h-3.5 w-3.5" /> : index + 1}
              </span>
              <span className={cn(index === step ? "font-medium text-foreground" : "text-muted-foreground")}>
                {title}
              </span>
              {index < STEPS.length - 1 && <span className="mx-1 h-px w-6 bg-border" />}
            </li>
          ))}
        </ol>
      )}

      <SwapIn k={String(step)}>
        {step === 0 && (
          <TemplateStep
            projectId={projectId}
            documentType={documentType}
            onReady={(m, name) => {
              // A different template means different fields and different
              // table columns; values typed against the old one would ride
              // along invisibly -- ghost rows that look empty but are counted
              // and sent.
              if (manifest && manifest.id !== m.id) {
                setValues({});
                setRows([{}]);
              }
              setManifest(m);
              setBlueprintName(name);
              setStep(1);
            }}
          />
        )}

        {step === 1 && (
          <div className="space-y-5 rounded-xl border border-border bg-card p-5">
            <div>
              <h2 className="text-base font-semibold text-foreground">Which study is this about?</h2>
              <p className="text-sm text-muted-foreground">
                {blueprintName ? `Template: ${blueprintName}. ` : ""}
                Pick from the study book, or type a one-off study.
              </p>
            </div>
            <div className="flex flex-wrap items-end gap-3">
              <div className="min-w-56 flex-1">
                <label className="mb-1.5 block text-sm font-medium">Study</label>
                <select
                  className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
                  value={studyId ?? ""}
                  onChange={(e) => setStudyId(e.target.value || null)}
                >
                  <option value="">— One-off (type details below) —</option>
                  {(studies ?? []).map((study) => (
                    <option key={study.id} value={study.id}>
                      {study.protocol_number}{study.title ? ` — ${study.title}` : ""}
                    </option>
                  ))}
                </select>
              </div>
              <Button variant="outline" onClick={() => setAddingStudy(true)}>New study</Button>
            </div>
            {selectedStudy ? (
              <div className="rounded-lg border border-border bg-muted/30 p-3 text-sm text-muted-foreground">
                <div className="font-medium text-foreground">{selectedStudy.protocol_number}</div>
                {selectedStudy.title && <div>{selectedStudy.title}</div>}
                {selectedStudy.sponsor && <div>Sponsor: {selectedStudy.sponsor}</div>}
                {selectedStudy.principal_investigator && (
                  <div>PI: {selectedStudy.principal_investigator}</div>
                )}
              </div>
            ) : (
              <div className="grid gap-3 rounded-lg border border-border bg-muted/20 p-3 sm:grid-cols-2">
                <Input placeholder="Protocol number" value={oneOff.protocol_number}
                       onChange={(e) => setOneOff({ ...oneOff, protocol_number: e.target.value })} />
                <Input placeholder="Study title" value={oneOff.title}
                       onChange={(e) => setOneOff({ ...oneOff, title: e.target.value })} />
                <Input placeholder="Sponsor" value={oneOff.sponsor}
                       onChange={(e) => setOneOff({ ...oneOff, sponsor: e.target.value })} />
                <Input placeholder="Principal investigator" value={oneOff.principal_investigator}
                       onChange={(e) => setOneOff({ ...oneOff, principal_investigator: e.target.value })} />
              </div>
            )}
            <div className="grid gap-4 sm:grid-cols-3">
              <div>
                <label className="mb-1.5 block text-sm font-medium">Document date</label>
                <Input type="date" value={documentDate} onChange={(e) => setDocumentDate(e.target.value)} />
              </div>
              <div>
                <label className="mb-1.5 block text-sm font-medium">Version</label>
                <Input value={versionLabel} onChange={(e) => setVersionLabel(e.target.value)} placeholder="1.0" />
              </div>
              <div>
                <label className="mb-1.5 block text-sm font-medium">Title (optional)</label>
                <Input value={docTitle} onChange={(e) => setDocTitle(e.target.value)}
                       placeholder={`${documentType} …`} />
              </div>
            </div>
            <StepNav onBack={() => setStep(0)} onNext={() => setStep(2)} />
          </div>
        )}

        {step === 2 && manifest && (
          <div className="space-y-5 rounded-xl border border-border bg-card p-5">
            <div>
              <h2 className="text-base font-semibold text-foreground">Fill in the values</h2>
              <p className="text-sm text-muted-foreground">
                One input per template field. Study identity, the document number and any table
                totals are filled by the server — nothing here asks you to retype them.
              </p>
            </div>
            <FieldValueForm
              fields={scalarFields}
              spec={spec}
              values={values}
              onValues={setValues}
              rows={rows}
              onRows={setRows}
              serverComputed={CLINICAL_COMPUTED}
              minRowNote="This table needs at least one row"
            />
            {spec && (
              <div className="flex justify-end">
                <div className="rounded-lg border border-border bg-muted/30 px-4 py-2 text-right text-sm tabular-nums">
                  <div className="text-muted-foreground">Rows {rowPreview.count}</div>
                  {rowPreview.sums.map((sum) => (
                    <div key={sum.label} className="text-muted-foreground">
                      Total {sum.label} <span className="font-medium text-foreground">{sum.value}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            <StepNav onBack={() => setStep(1)} onNext={() => setStep(3)} />
          </div>
        )}

        {step === 3 && manifest && (
          <ReviewStep
            studyName={
              selectedStudy?.protocol_number
              ?? (oneOff.protocol_number.trim() || "— no study yet —")
            }
            documentType={documentType}
            rowCount={rowPreview.count}
            hasTable={spec !== null}
            requiredRows={spec?.required === true}
            onBack={() => setStep(2)}
            onGenerate={generate}
          />
        )}

        {step === 4 && result && (
          <SuccessPanel
            result={result}
            onAnother={() => {
              setStep(1);
              setRows([{}]);
              setResult(null);
            }}
            onDone={onViewAll}
          />
        )}
      </SwapIn>

      <StudyDialog
        editing={addingStudy ? "new" : null}
        onClose={() => setAddingStudy(false)}
        onSaved={async (saved) => {
          setAddingStudy(false);
          const res = await api.listStudies().catch(() => null);
          if (res) setStudies(res.items);
          setStudyId(saved.id);
        }}
      />
    </div>
  );
}

function StepNav({ onBack, onNext, nextLabel = "Continue" }: {
  onBack: () => void; onNext: () => void; nextLabel?: string;
}) {
  return (
    <div className="flex justify-between border-t border-border pt-4">
      <Button variant="ghost" onClick={onBack}><ArrowLeft className="mr-1.5 h-4 w-4" /> Back</Button>
      <Button onClick={onNext}>{nextLabel} <ArrowRight className="ml-1.5 h-4 w-4" /></Button>
    </div>
  );
}

/* ------------------------------------------------------------------ step 1 */

function TemplateStep({ projectId, documentType, onReady }: {
  projectId: string;
  documentType: string;
  onReady: (manifest: ManifestData, blueprintName: string) => void;
}) {
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ title: string; detail: string } | null>(null);
  const [generated, setGenerated] = useState<{ blueprint: Blueprint & { generation?: { source: string; notes: string[] } } } | null>(null);
  const [existing, setExisting] = useState<Blueprint[] | null>(null);
  const { service } = serviceFor(documentType);

  useEffect(() => {
    let live = true;
    api.listBlueprints(projectId)
      .then((res) => { if (live) setExisting(res.items); })
      .catch(() => { if (live) setExisting([]); });
    return () => { live = false; };
  }, [projectId]);

  async function describe() {
    if (!description.trim()) {
      toast.error("Describe the study first — a sentence or two is plenty.");
      return;
    }
    setBusy("describe");
    setError(null);
    try {
      const blueprint = await api.blueprintFromDescription({
        description,
        service,
        project_id: projectId,
        name: `${documentType} template`,
      });
      setGenerated({ blueprint });
      if (blueprint.generation?.source === "kit_fallback") {
        toast.info("Using a ready-made template", {
          description: "You can tweak it in the studio before you use it.",
        });
      }
      const res = await api.listBlueprints(projectId).catch(() => null);
      if (res) setExisting(res.items);
    } catch (e: any) {
      setError({ title: "The template could not be authored", detail: plainly(String(e?.message ?? e)) });
    } finally {
      setBusy(null);
    }
  }

  async function manifestFor(blueprint: Blueprint): Promise<ManifestData> {
    // A published blueprint already has a manifest; a draft is published here,
    // through the same lint gate every template passes.
    if (blueprint.status === "published" && blueprint.template_file_id) {
      const manifests = await api.listManifests(blueprint.template_file_id);
      const usable = (manifests.items ?? [])
        .filter((m: any) => !["superseded", "deprecated", "failed"].includes(m.status))
        .sort((a: any, b: any) => (b.version_no ?? 0) - (a.version_no ?? 0))[0];
      if (usable) {
        const full = await api.getManifest(usable.id);
        return { id: usable.id, fields: full.fields ?? [], blocks: full.blocks ?? [] };
      }
    }
    const published = await api.publishBlueprint(blueprint.id, [], false);
    const full = await api.getManifest(published.manifest_id);
    return { id: published.manifest_id, fields: full.fields ?? [], blocks: full.blocks ?? [] };
  }

  async function use(blueprint: Blueprint) {
    setBusy(blueprint.id);
    setError(null);
    try {
      const manifest = await manifestFor(blueprint);
      onReady(manifest, blueprint.name);
    } catch (e: any) {
      if (e instanceof ApiError && e.code === "BLUEPRINT_NOT_PUBLISHABLE") {
        setError({
          title: "This template is not ready to publish",
          detail: `${plainly(String(e.message))} — open it in the studio to fix, then come back.`,
        });
      } else {
        setError({ title: "This template could not be used", detail: plainly(String(e?.message ?? e)) });
      }
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-5">
      <div className="rounded-xl border border-border bg-card p-5">
        <div className="mb-3 flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-brand" />
          <h2 className="text-base font-semibold text-foreground">Describe the study</h2>
        </div>
        <p className="mb-3 text-sm text-muted-foreground">
          The therapy area, the phase, the sites — anything the {documentType.toLowerCase()} must
          carry. The AI drafts a complete template you can tweak in the studio.
        </p>
        <Textarea
          rows={3}
          placeholder="A phase 2 oncology study of drug X across 12 sites in India, sponsored by Acme Pharma."
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <div className="mt-3 flex justify-end">
          <Button onClick={describe} disabled={busy !== null}>
            <Wand2 className="mr-1.5 h-4 w-4" />
            {busy === "describe" ? "Drafting…" : "Draft my template"}
          </Button>
        </div>
        {busy === "describe" && <StageSkeleton lines={3} />}
        {generated && (
          <div className="mt-4 rounded-lg border border-success/40 bg-success/10 p-4">
            <div className="mb-1 flex items-center gap-2 text-sm font-medium text-foreground">
              <Check className="h-4 w-4 text-success" />
              {generated.blueprint.name} is drafted
            </div>
            {(generated.blueprint.generation?.notes ?? []).map((note, i) => (
              <p key={i} className="text-xs text-muted-foreground">{plainly(String(note))}</p>
            ))}
            <div className="mt-3 flex flex-wrap gap-2">
              <Button size="sm" onClick={() => use(generated.blueprint)} disabled={busy !== null}>
                {busy === generated.blueprint.id ? "Publishing…" : "Use this template"}
              </Button>
              <Button size="sm" variant="outline" asChild>
                <Link to="/templates/$blueprintId" params={{ blueprintId: generated.blueprint.id }}>
                  <Pencil className="mr-1.5 h-3.5 w-3.5" /> Tweak in studio
                </Link>
              </Button>
            </div>
          </div>
        )}
      </div>

      {error && <ErrorBanner title={error.title} message="Nothing was created." detail={error.detail} />}

      <div className="rounded-xl border border-border bg-card p-5">
        <h2 className="mb-3 text-base font-semibold text-foreground">Or use a saved template</h2>
        {existing === null ? (
          <StageSkeleton lines={2} />
        ) : existing.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Templates you draft or tweak appear here for the next document.
          </p>
        ) : (
          <ul className="divide-y divide-border/60">
            {existing.map((blueprint) => (
              <li key={blueprint.id} className="flex items-center justify-between gap-3 py-2.5">
                <div className="flex items-center gap-2 text-sm">
                  <FileText className="h-4 w-4 text-muted-foreground" />
                  <span className="font-medium text-foreground">{blueprint.name}</span>
                  <span className="text-xs text-muted-foreground capitalize">{blueprint.status}</span>
                </div>
                <div className="flex gap-2">
                  <Button size="sm" variant="outline" asChild>
                    <Link to="/templates/$blueprintId" params={{ blueprintId: blueprint.id }}>Edit</Link>
                  </Button>
                  <Button size="sm" onClick={() => use(blueprint)} disabled={busy !== null}>
                    {busy === blueprint.id ? "Preparing…" : "Use"}
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ steps 3-4 */

function ReviewStep({ studyName, documentType, rowCount, hasTable, requiredRows, onBack, onGenerate }: {
  studyName: string;
  documentType: string;
  rowCount: number;
  hasTable: boolean;
  requiredRows: boolean;
  onBack: () => void;
  onGenerate: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <div className="space-y-5 rounded-xl border border-border bg-card p-5">
      <div>
        <h2 className="text-base font-semibold text-foreground">Ready to generate</h2>
        <p className="text-sm text-muted-foreground">
          The number is allocated when the document is stored — never before, so a failed
          generation cannot burn one.
        </p>
      </div>
      <dl className="grid gap-3 text-sm sm:grid-cols-3">
        <div className="rounded-lg border border-border p-3">
          <dt className="text-muted-foreground">Study</dt>
          <dd className="font-medium text-foreground">{studyName}</dd>
        </div>
        <div className="rounded-lg border border-border p-3">
          <dt className="text-muted-foreground">Document</dt>
          <dd className="font-medium text-foreground">{documentType}</dd>
        </div>
        <div className="rounded-lg border border-border p-3">
          <dt className="text-muted-foreground">Table rows</dt>
          <dd className="font-medium tabular-nums text-foreground">{hasTable ? rowCount : "—"}</dd>
        </div>
      </dl>
      <div className="flex justify-between border-t border-border pt-4">
        <Button variant="ghost" onClick={onBack} disabled={busy}>
          <ArrowLeft className="mr-1.5 h-4 w-4" /> Back
        </Button>
        <Button
          onClick={async () => { setBusy(true); try { await onGenerate(); } finally { setBusy(false); } }}
          disabled={busy || (requiredRows && rowCount === 0)}
        >
          <Stethoscope className="mr-1.5 h-4 w-4" />
          {busy ? "Generating…" : "Generate document"}
        </Button>
      </div>
    </div>
  );
}

function SuccessPanel({ result, onAnother, onDone }: {
  result: NonNullable<Awaited<ReturnType<typeof api.generateClinicalDocument>>>;
  onAnother: () => void;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState<"docx" | "pdf" | null>(null);

  async function download(format: "docx" | "pdf") {
    if (!result.document_version_id) return;
    setBusy(format);
    try {
      const url = await api.downloadVersion(result.document_version_id, format);
      await saveBlob(url, `${result.number}.${format}`);
    } catch (e: any) {
      toast.error(format === "pdf" ? "PDF is not available on this server" : "Download failed", {
        description: plainly(String(e?.message ?? e)),
      });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-5 rounded-xl border border-success/40 bg-success/5 p-6 text-center">
      <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-success/15">
        <Check className="h-6 w-6 text-success" />
      </div>
      <div>
        <h2 className="text-xl font-semibold text-foreground">{result.number}</h2>
        <p className="text-sm text-muted-foreground">
          {!result.qa_passed
            ? "Generated, but its checks found problems — it is stored as a draft."
            : result.approval_note
              ? "Generated and checked — waiting for sign-off."
              : "Generated, checked, and stored."}
        </p>
      </div>
      {result.qa_passed && result.approval_note && (
        <p className="mx-auto max-w-lg rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-foreground">
          {result.approval_note} Downloads unlock once it is approved.
        </p>
      )}
      {!result.qa_passed && result.qa_notes.length > 0 && (
        <ul className="mx-auto max-w-lg space-y-1 text-left text-xs text-ai-blocked">
          {qaNoteLines(result.qa_notes).slice(0, 4).map((note, i) => <li key={i}>• {note}</li>)}
        </ul>
      )}
      <div className="flex flex-wrap justify-center gap-2">
        <Button onClick={() => download("docx")} disabled={busy !== null}>
          <Download className="mr-1.5 h-4 w-4" /> {busy === "docx" ? "Preparing…" : "DOCX"}
        </Button>
        <Button variant="outline" onClick={() => download("pdf")} disabled={busy !== null}>
          <FileText className="mr-1.5 h-4 w-4" /> {busy === "pdf" ? "Converting…" : "PDF"}
        </Button>
      </div>
      <div className="flex justify-center gap-4 text-sm">
        <button className="text-brand hover:underline" onClick={onAnother}>Another document with this template</button>
        <button className="text-muted-foreground hover:underline" onClick={onDone}>All documents</button>
      </div>
    </div>
  );
}
