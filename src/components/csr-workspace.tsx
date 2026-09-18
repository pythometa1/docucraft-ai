/**
 * The CSR module (milestone M1), living where the user already works: a
 * Clinical project whose document type is "Clinical Study Report" renders
 * this instead of the template studio.
 *
 * M1 is the front door: study details (S1), template choice (S2), and the
 * seeded ICH E3 section tree. Source upload (S3/S4), grounded generation
 * (S5) and QC/export (S6) arrive milestone by milestone -- the UI says so
 * instead of pretending.
 *
 * Positioning, everywhere a draft will appear: this is an AI-ASSISTED
 * DRAFTING TOOL for medical writers. Sections require human review and
 * approval; nothing exports unapproved.
 */

import { useEffect, useState } from "react";
import {
  AlertTriangle, Check, ChevronRight, FileText, FlaskConical, ListTree,
  Trash2, Wand2,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type { CsrProject, CsrReadiness, CsrSection, Study } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ErrorBanner } from "@/components/error-banner";
import { StageSkeleton } from "@/components/skeletons";
import { SwapIn } from "@/components/motion";
import { StudyDialog } from "@/components/clinical-studio";
import { CsrSources } from "@/components/csr-sources";
import { CsrEditor } from "@/components/csr-editor";
import { cn } from "@/lib/utils";

const BLINDING_OPTIONS = [
  ["open_label", "Open label"],
  ["single_blind", "Single blind"],
  ["double_blind", "Double blind"],
] as const;

const STATUS_LABEL: Record<string, string> = {
  not_started: "Not started", generating: "Generating…", draft: "Draft",
  in_review: "In review", approved: "Approved",
};

function Disclaimer() {
  return (
    <p className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-foreground">
      AI-assisted drafting tool for medical writers. Every section requires human review
      and approval before it can be exported — this tool never writes a CSR on its own.
    </p>
  );
}

export function CsrWorkspace({ project }: { project: { id: string; name: string } }) {
  const [csr, setCsr] = useState<CsrProject | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    let live = true;
    api.csrListProjects()
      .then((res) => {
        if (!live) return;
        setCsr(res.items.find((p) => p.project_id === project.id) ?? null);
      })
      .catch((e) => { if (live) { setError(e?.message ?? String(e)); setCsr(null); } });
    return () => { live = false; };
  }, [project.id, refresh]);

  if (csr === undefined) return <StageSkeleton lines={4} />;
  if (error) return <ErrorBanner title="The CSR could not be loaded" message="Try again in a moment." detail={error} />;

  return (
    <div className="space-y-4">
      <Disclaimer />
      {csr === null ? (
        <CsrWizard projectId={project.id} onCreated={() => setRefresh((n) => n + 1)} />
      ) : (
        <CsrOverview csr={csr} onChanged={() => setRefresh((n) => n + 1)} />
      )}
    </div>
  );
}

/* ------------------------------------------------------------ wizard S1-S2 */

function CsrWizard({ projectId, onCreated }: { projectId: string; onCreated: () => void }) {
  const [step, setStep] = useState(0);

  const [studies, setStudies] = useState<Study[] | null>(null);
  const [studyId, setStudyId] = useState<string | null>(null);
  const [addingStudy, setAddingStudy] = useState(false);
  const [oneOff, setOneOff] = useState({
    protocol_number: "", title: "", sponsor: "", phase: "", indication: "",
    principal_investigator: "",
  });
  const [compound, setCompound] = useState("");
  const [therapeuticArea, setTherapeuticArea] = useState("");
  const [blinding, setBlinding] = useState<string>("double_blind");
  const [designSummary, setDesignSummary] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api.listStudies()
      .then((res) => { if (live) setStudies(res.items); })
      .catch(() => { if (live) setStudies([]); });
    return () => { live = false; };
  }, []);

  const selectedStudy = studies?.find((s) => s.id === studyId) ?? null;

  async function create() {
    if (!studyId && !oneOff.protocol_number.trim()) {
      toast.error("A CSR is about a study — pick one or type at least a protocol number.");
      setStep(0);
      return;
    }
    setBusy(true);
    try {
      const made = await api.csrCreateProject({
        project_id: projectId,
        study_id: studyId ?? undefined,
        study: studyId ? undefined : {
          protocol_number: oneOff.protocol_number.trim(),
          title: oneOff.title.trim() || undefined,
          sponsor: oneOff.sponsor.trim() || undefined,
          phase: oneOff.phase.trim() || undefined,
          indication: oneOff.indication.trim() || undefined,
          principal_investigator: oneOff.principal_investigator.trim() || undefined,
        },
        compound_name: compound.trim() || undefined,
        therapeutic_area: therapeuticArea.trim() || undefined,
        blinding,
        study_design_summary: designSummary.trim() || undefined,
      });
      await api.csrChooseTemplate(made.id, "builtin_ich_e3");
      toast.success("CSR project created — the ICH E3 section tree is ready.");
      onCreated();
    } catch (e: any) {
      toast.error("The CSR project could not be created", { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <ol className="flex items-center gap-2 text-sm">
        {["Study details", "Template"].map((title, index) => (
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
            {index === 0 && <span className="mx-1 h-px w-6 bg-border" />}
          </li>
        ))}
      </ol>

      <SwapIn k={String(step)}>
        {step === 0 && (
          <div className="space-y-5 rounded-xl border border-border bg-card p-5">
            <div>
              <h2 className="text-base font-semibold text-foreground">Which study is this report about?</h2>
              <p className="text-sm text-muted-foreground">
                Everything entered here becomes generation metadata for every section.
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
              </div>
            ) : (
              <div className="grid gap-3 rounded-lg border border-border bg-muted/20 p-3 sm:grid-cols-3">
                <Input placeholder="Protocol number" value={oneOff.protocol_number}
                       onChange={(e) => setOneOff({ ...oneOff, protocol_number: e.target.value })} />
                <Input placeholder="Study title" value={oneOff.title}
                       onChange={(e) => setOneOff({ ...oneOff, title: e.target.value })} />
                <Input placeholder="Sponsor" value={oneOff.sponsor}
                       onChange={(e) => setOneOff({ ...oneOff, sponsor: e.target.value })} />
                <Input placeholder="Phase (1, 2, 3, 4)" value={oneOff.phase}
                       onChange={(e) => setOneOff({ ...oneOff, phase: e.target.value })} />
                <Input placeholder="Indication" value={oneOff.indication}
                       onChange={(e) => setOneOff({ ...oneOff, indication: e.target.value })} />
                <Input placeholder="Principal investigator" value={oneOff.principal_investigator}
                       onChange={(e) => setOneOff({ ...oneOff, principal_investigator: e.target.value })} />
              </div>
            )}
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <label className="mb-1.5 block text-sm font-medium">Compound name</label>
                <Input value={compound} onChange={(e) => setCompound(e.target.value)} placeholder="Drug X" />
              </div>
              <div>
                <label className="mb-1.5 block text-sm font-medium">Therapeutic area</label>
                <Input value={therapeuticArea} onChange={(e) => setTherapeuticArea(e.target.value)} placeholder="Oncology" />
              </div>
              <div>
                <label className="mb-1.5 block text-sm font-medium">Blinding</label>
                <select
                  className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
                  value={blinding}
                  onChange={(e) => setBlinding(e.target.value)}
                >
                  {BLINDING_OPTIONS.map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
              </div>
              <div className="sm:col-span-2">
                <label className="mb-1.5 block text-sm font-medium">Study design summary</label>
                <Textarea rows={2} value={designSummary}
                          onChange={(e) => setDesignSummary(e.target.value)}
                          placeholder="Randomised, double-blind, placebo-controlled, 12 sites…" />
              </div>
            </div>
            <div className="flex justify-end border-t border-border pt-4">
              <Button onClick={() => setStep(1)}>Continue <ChevronRight className="ml-1 h-4 w-4" /></Button>
            </div>
          </div>
        )}

        {step === 1 && (
          <div className="space-y-5 rounded-xl border border-border bg-card p-5">
            <div>
              <h2 className="text-base font-semibold text-foreground">Report structure</h2>
              <p className="text-sm text-muted-foreground">
                The section order is fixed by the ICH E3 numbering; you can disable
                sections that do not apply after creation.
              </p>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-lg border-2 border-brand bg-brand/5 p-4">
                <div className="mb-1 flex items-center gap-2 text-sm font-semibold text-foreground">
                  <ListTree className="h-4 w-4 text-brand" /> Standard ICH E3 template
                </div>
                <p className="text-xs text-muted-foreground">
                  Sections 1–16 with the full 9.x / 11.4.x / 12.2.x subsection structure,
                  each carrying its drafting guidance.
                </p>
              </div>
              <div className="rounded-lg border border-border p-4 opacity-60">
                <div className="mb-1 flex items-center gap-2 text-sm font-semibold text-foreground">
                  <FileText className="h-4 w-4" /> Upload sponsor template
                </div>
                <p className="text-xs text-muted-foreground">
                  Parse a sponsor DOCX's heading tree into the section list — arrives in a
                  later milestone.
                </p>
              </div>
            </div>
            <div className="flex justify-between border-t border-border pt-4">
              <Button variant="ghost" onClick={() => setStep(0)}>Back</Button>
              <Button onClick={create} disabled={busy}>
                <Wand2 className="mr-1.5 h-4 w-4" />
                {busy ? "Creating…" : "Create CSR project"}
              </Button>
            </div>
          </div>
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

/* ------------------------------------------------------------ overview */

function CsrOverview({ csr, onChanged }: { csr: CsrProject; onChanged: () => void }) {
  const [tab, setTab] = useState<"sources" | "write" | "structure">("sources");
  const [sections, setSections] = useState<CsrSection[] | null>(null);
  const [readiness, setReadiness] = useState<CsrReadiness | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.csrSections(csr.id)
      .then((res) => { if (live) setSections(res.items); })
      .catch(() => { if (live) setSections([]); });
    return () => { live = false; };
  }, [csr.id]);

  // The writing tab only means anything once the required sources are indexed:
  // a section drafted from nothing is a section of [DATA NEEDED].
  useEffect(() => {
    if (readiness?.ready_to_generate && tab === "sources" && sections?.some((s) => s.status !== "not_started")) {
      setTab("write");
    }
  }, [readiness?.ready_to_generate]);

  async function toggle(section: CsrSection) {
    setBusy(section.id);
    try {
      const updated = await api.csrToggleSection(section.id, !section.enabled);
      setSections((prev) => (prev ?? []).map((s) => (s.id === section.id ? updated : s)));
    } catch (e: any) {
      toast.error("Could not toggle this section", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function purge() {
    if (!window.confirm(
      "Delete this CSR project? Its uploaded sources, their index, every draft and "
      + "its section tree are purged. The portal project itself remains.")) return;
    setBusy("purge");
    try {
      await api.csrDeleteProject(csr.id);
      toast.success("CSR project purged.");
      onChanged();
    } catch (e: any) {
      toast.error("Could not delete this CSR project", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  const depth = (n: string) => n.split(".").length - 1;
  const approved = (sections ?? []).filter((s) => !s.is_container && s.enabled && s.status === "approved").length;
  const writable = (sections ?? []).filter((s) => !s.is_container && s.enabled).length;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-border bg-card p-4">
        <div className="space-y-1 text-sm">
          <div className="flex items-center gap-2 font-semibold text-foreground">
            <FlaskConical className="h-4 w-4 text-brand" />
            {csr.study?.protocol_number ?? "\u2014"}
            {csr.compound_name && <span className="text-muted-foreground">\u00b7 {csr.compound_name}</span>}
          </div>
          {csr.study?.title && <div className="text-muted-foreground">{csr.study.title}</div>}
          <div className="text-xs text-muted-foreground">
            {[csr.study?.sponsor, csr.study?.phase && `Phase ${csr.study.phase}`,
              csr.study?.indication, csr.therapeutic_area,
              BLINDING_OPTIONS.find(([v]) => v === csr.blinding)?.[1]]
              .filter(Boolean).join(" \u00b7 ")}
          </div>
        </div>
        <div className="flex items-center gap-3">
          {writable > 0 && (
            <div className="text-right text-xs">
              <div className="text-muted-foreground">Approved</div>
              <div className="text-sm font-semibold text-foreground">{approved}/{writable}</div>
            </div>
          )}
          <Button variant="outline" onClick={purge} disabled={busy !== null}
                  className="text-destructive hover:bg-destructive/10">
            <Trash2 className="mr-1.5 h-4 w-4" /> Delete CSR
          </Button>
        </div>
      </div>

      <div className="flex gap-1 border-b border-border">
        {([["sources", "Source documents", FileText],
           ["write", "Write the report", Wand2],
           ["structure", "Structure", ListTree]] as const).map(([key, title, Icon]) => (
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
        {tab === "sources" && (
          <CsrSources csrProjectId={csr.id} onReadiness={setReadiness} />
        )}

        {tab === "write" && (
          sections === null ? <StageSkeleton lines={5} />
            : !readiness?.ready_to_generate ? (
              <div className="space-y-3 rounded-xl border border-border bg-card p-6 text-center">
                <AlertTriangle className="mx-auto h-6 w-6 text-warning" />
                <p className="text-sm text-foreground">
                  The required sources are not indexed yet.
                </p>
                <p className="text-xs text-muted-foreground">
                  Every section is written only from what you upload. Add the protocol,
                  the statistical analysis plan and the statistical outputs, process them,
                  then come back \u2014 a section drafted from nothing is a section of gaps.
                </p>
                <Button variant="outline" onClick={() => setTab("sources")}>Go to source documents</Button>
              </div>
            ) : (
              <CsrEditor
                csrProjectId={csr.id}
                sections={sections}
                onSectionsChanged={setSections}
              />
            )
        )}

        {tab === "structure" && (
          <div className="rounded-xl border border-border bg-card">
            <div className="border-b border-border px-4 py-3 text-sm font-semibold text-foreground">
              ICH E3 sections
            </div>
            {sections === null ? (
              <div className="p-4"><StageSkeleton lines={4} /></div>
            ) : (
              <ul className="divide-y divide-border/60">
                {sections.map((section) => (
                  <li key={section.id}
                      className={cn("flex items-center justify-between gap-3 px-4 py-2 text-sm",
                                    !section.enabled && "opacity-50")}>
                    <div className="flex min-w-0 items-baseline gap-2"
                         style={{ paddingLeft: `${depth(section.section_number) * 16}px` }}>
                      <span className="font-mono text-xs text-muted-foreground">{section.section_number}</span>
                      <span className={cn("truncate", section.is_container ? "font-semibold text-foreground" : "text-foreground")}>
                        {section.title}
                      </span>
                    </div>
                    <div className="flex shrink-0 items-center gap-3">
                      {!section.is_container && (
                        <>
                          <span className="text-xs text-muted-foreground">{STATUS_LABEL[section.status] ?? section.status}</span>
                          <button
                            onClick={() => toggle(section)}
                            disabled={busy !== null}
                            className={cn(
                              "rounded-full border px-2 py-0.5 text-xs font-medium transition-colors",
                              section.enabled
                                ? "border-success/40 bg-success/10 text-success"
                                : "border-border text-muted-foreground hover:text-foreground",
                            )}
                            title={section.enabled ? "Included in the report \u2014 click to exclude" : "Excluded \u2014 click to include"}
                          >
                            {section.enabled ? "Included" : "Excluded"}
                          </button>
                        </>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </SwapIn>
    </div>
  );
}
