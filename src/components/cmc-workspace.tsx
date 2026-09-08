/**
 * The Quality/CMC module, living where the user already works: a project
 * whose function is Quality-CMC renders this instead of the generic pipeline.
 *
 * An AI-ASSISTED DRAFTING TOOL for CMC and regulatory writers. Prose sections
 * are drafted from the uploaded sources and cited; the numbers in a
 * specification, a batch analysis or a stability table are never written by a
 * model at all -- they are read from the sources, checked by a person in the
 * data grid, and rendered from the store. That separation is the module, and
 * the tabs are ordered to walk it: set up, add sources, check the data, then
 * write.
 */

import { useEffect, useState } from "react";
import {
  AlertTriangle, Check, ChevronRight, FlaskConical, Layers, ListTree,
  Plus, ShieldQuestion, Trash2, Upload,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type {
  CmcDeliverableType, CmcDocument, CmcProject, CmcReadiness, CmcSection,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ErrorBanner } from "@/components/error-banner";
import { StageSkeleton } from "@/components/skeletons";
import { SwapIn } from "@/components/motion";
import { CmcDataGrid } from "@/components/cmc-data-grid";
import { CmcSources } from "@/components/cmc-sources";
import { cn } from "@/lib/utils";

const SUBMISSION_TYPES = ["IND", "IMPD", "NDA", "ANDA", "MAA", "variation", "other"];
const REGIONS = ["FDA", "EMA", "CDSCO", "PMDA", "HC", "other"];

const APPLICABILITY_LABEL: Record<string, string> = {
  applicable: "Applicable",
  not_applicable: "Not applicable",
  referenced_dmf: "Referenced DMF",
};

function Disclaimer() {
  return (
    <p className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-foreground">
      AI-assisted drafting tool for CMC and regulatory writers. Sections require human review and
      approval before export. Specification, batch and stability tables are rendered from data you
      have verified — no model writes a number into this dossier.
    </p>
  );
}

export function CmcWorkspace({ project }: { project: { id: string; name: string } }) {
  const [cmc, setCmc] = useState<CmcProject | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    let live = true;
    api.cmcListProjects()
      .then((res) => {
        if (!live) return;
        setCmc(res.items.find((p) => p.project_id === project.id) ?? null);
      })
      .catch((e) => { if (live) { setError(e?.message ?? String(e)); setCmc(null); } });
    return () => { live = false; };
  }, [project.id, refresh]);

  if (cmc === undefined) return <StageSkeleton lines={4} />;
  if (error) return <ErrorBanner title="The dossier could not be loaded" message="Try again in a moment." detail={error} />;

  return (
    <div className="space-y-4">
      <Disclaimer />
      {cmc === null
        ? <CmcSetup projectId={project.id} onCreated={() => setRefresh((n) => n + 1)} />
        : <CmcOverview cmc={cmc} onChanged={() => setRefresh((n) => n + 1)} />}
    </div>
  );
}

/* ------------------------------------------------------------ setup (S1) */

function CmcSetup({ projectId, onCreated }: { projectId: string; onCreated: () => void }) {
  const [form, setForm] = useState({
    product_name: "", inn_or_ds_name: "", dosage_form: "",
    strengths: "", route_of_administration: "",
    submission_type: "NDA", development_phase: "",
  });
  const [regions, setRegions] = useState<string[]>(["FDA"]);
  const [busy, setBusy] = useState(false);

  async function create() {
    if (!form.product_name.trim()) {
      toast.error("A quality dossier needs a product name.");
      return;
    }
    setBusy(true);
    try {
      await api.cmcCreateProject({
        project_id: projectId,
        product_name: form.product_name.trim(),
        inn_or_ds_name: form.inn_or_ds_name.trim() || undefined,
        dosage_form: form.dosage_form.trim() || undefined,
        strengths: form.strengths.split(",").map((s) => s.trim()).filter(Boolean),
        route_of_administration: form.route_of_administration.trim() || undefined,
        submission_type: form.submission_type,
        target_regions: regions,
        development_phase: form.development_phase.trim() || undefined,
      });
      toast.success("Dossier created — choose what you are writing next.");
      onCreated();
    } catch (e: any) {
      toast.error("The dossier could not be created", { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5 rounded-xl border border-border bg-card p-5">
      <div>
        <h2 className="text-base font-semibold text-foreground">What is this dossier about?</h2>
        <p className="text-sm text-muted-foreground">
          Everything here becomes generation metadata for every section, and the target regions
          decide which regional items 3.2.R asks for.
        </p>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label className="mb-1.5 block text-sm font-medium">Product name</label>
          <Input value={form.product_name} placeholder="Drug X Tablets"
                 onChange={(e) => setForm({ ...form, product_name: e.target.value })} />
        </div>
        <div>
          <label className="mb-1.5 block text-sm font-medium">Drug substance (INN)</label>
          <Input value={form.inn_or_ds_name} placeholder="drugxinib"
                 onChange={(e) => setForm({ ...form, inn_or_ds_name: e.target.value })} />
        </div>
        <div>
          <label className="mb-1.5 block text-sm font-medium">Dosage form</label>
          <Input value={form.dosage_form} placeholder="Film-coated tablet"
                 onChange={(e) => setForm({ ...form, dosage_form: e.target.value })} />
        </div>
        <div>
          <label className="mb-1.5 block text-sm font-medium">Strengths</label>
          <Input value={form.strengths} placeholder="50 mg, 100 mg"
                 onChange={(e) => setForm({ ...form, strengths: e.target.value })} />
        </div>
        <div>
          <label className="mb-1.5 block text-sm font-medium">Route of administration</label>
          <Input value={form.route_of_administration} placeholder="Oral"
                 onChange={(e) => setForm({ ...form, route_of_administration: e.target.value })} />
        </div>
        <div>
          <label className="mb-1.5 block text-sm font-medium">Submission type</label>
          <select className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
                  value={form.submission_type}
                  onChange={(e) => setForm({ ...form, submission_type: e.target.value })}>
            {SUBMISSION_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
        <div className="sm:col-span-2">
          <label className="mb-1.5 block text-sm font-medium">Target regions</label>
          <div className="flex flex-wrap gap-2">
            {REGIONS.map((region) => (
              <button
                key={region}
                onClick={() => setRegions((prev) => prev.includes(region)
                  ? prev.filter((r) => r !== region) : [...prev, region])}
                className={cn(
                  "rounded-full border px-3 py-1 text-xs font-medium transition-colors",
                  regions.includes(region)
                    ? "border-brand bg-brand/10 text-brand"
                    : "border-border text-muted-foreground hover:text-foreground",
                )}
              >
                {region}
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="flex justify-end border-t border-border pt-4">
        <Button onClick={create} disabled={busy}>
          {busy ? "Creating…" : "Create dossier"} <ChevronRight className="ml-1 h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

/* --------------------------------------------------------- overview shell */

type Tab = "deliverables" | "sources" | "data" | "sites";

function CmcOverview({ cmc, onChanged }: { cmc: CmcProject; onChanged: () => void }) {
  const [tab, setTab] = useState<Tab>(cmc.deliverables.length ? "sources" : "deliverables");
  const [documents, setDocuments] = useState<CmcDocument[]>([]);
  const [readiness, setReadiness] = useState<CmcReadiness | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function purge() {
    if (!window.confirm(
      "Delete this quality dossier? Its uploaded sources, their index, every extracted "
      + "value and all section drafts are purged. The portal project itself remains.")) return;
    setBusy("purge");
    try {
      await api.cmcDeleteProject(cmc.id);
      toast.success("Dossier purged.");
      onChanged();
    } catch (e: any) {
      toast.error("Could not delete this dossier", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  const TABS: [Tab, string, typeof Layers][] = [
    ["deliverables", "Deliverables", Layers],
    ["sources", "Source documents", Upload],
    ["data", "Data review", ShieldQuestion],
    ["sites", "Sites", FlaskConical],
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-border bg-card p-4">
        <div className="space-y-1 text-sm">
          <div className="flex items-center gap-2 font-semibold text-foreground">
            <FlaskConical className="h-4 w-4 text-brand" />
            {cmc.product_name}
            {cmc.inn_or_ds_name && <span className="text-muted-foreground">· {cmc.inn_or_ds_name}</span>}
          </div>
          <div className="text-xs text-muted-foreground">
            {[cmc.dosage_form, cmc.strengths.join(", ") || null, cmc.route_of_administration,
              cmc.submission_type, cmc.target_regions.join(" / ") || null]
              .filter(Boolean).join(" · ")}
          </div>
        </div>
        <Button variant="outline" onClick={purge} disabled={busy !== null}
                className="text-destructive hover:bg-destructive/10">
          <Trash2 className="mr-1.5 h-4 w-4" /> Delete dossier
        </Button>
      </div>

      <div className="flex gap-1 border-b border-border">
        {TABS.map(([key, label, Icon]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={cn(
              "inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition-colors",
              tab === key ? "border-brand text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            <Icon className="h-4 w-4" /> {label}
          </button>
        ))}
      </div>

      <SwapIn k={tab}>
        {tab === "deliverables" && <Deliverables cmc={cmc} onChanged={onChanged} />}
        {tab === "sources" && (
          <CmcSources
            cmcProjectId={cmc.id}
            onLoaded={(docs, r) => { setDocuments(docs); setReadiness(r); }}
          />
        )}
        {tab === "data" && <CmcDataGrid cmcProjectId={cmc.id} documents={documents} />}
        {tab === "sites" && <Sites cmc={cmc} onChanged={onChanged} />}
      </SwapIn>

      {tab === "sources" && readiness && !readiness.ready_to_generate && (
        <p className="flex items-center gap-2 text-xs text-muted-foreground">
          <AlertTriangle className="h-3.5 w-3.5" />
          Sections cannot be drafted until the required sources are indexed and their values checked.
        </p>
      )}
    </div>
  );
}

/* ---------------------------------------------------- deliverables (S2) */

function Deliverables({ cmc, onChanged }: { cmc: CmcProject; onChanged: () => void }) {
  const [types, setTypes] = useState<CmcDeliverableType[] | null>(null);
  const [sections, setSections] = useState<Record<string, CmcSection[]>>({});
  const [open, setOpen] = useState<string | null>(cmc.deliverables[0]?.id ?? null);
  const [busy, setBusy] = useState<string | null>(null);
  const [justifying, setJustifying] = useState<{ id: string; applicability: string } | null>(null);
  const [justification, setJustification] = useState("");

  useEffect(() => {
    let live = true;
    api.cmcDeliverableTypes()
      .then((res) => { if (live) setTypes(res.items); })
      .catch(() => { if (live) setTypes([]); });
    return () => { live = false; };
  }, []);

  useEffect(() => {
    if (!open) return;
    let live = true;
    api.cmcDeliverableSections(open)
      .then((res) => { if (live) setSections((prev) => ({ ...prev, [open]: res.items })); })
      .catch(() => undefined);
    return () => { live = false; };
  }, [open]);

  async function add(key: string) {
    setBusy(key);
    try {
      await api.cmcAddDeliverable(cmc.id, key);
      toast.success("Added — its section tree is ready.");
      onChanged();
    } catch (e: any) {
      toast.error("Could not add this deliverable", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function patch(section: CmcSection, body: Record<string, unknown>) {
    setBusy(section.id);
    try {
      const updated = await api.cmcPatchSection(section.id, body);
      setSections((prev) => ({
        ...prev,
        [open!]: (prev[open!] ?? []).map((s) => (s.id === section.id ? updated : s)),
      }));
      setJustifying(null);
      setJustification("");
    } catch (e: any) {
      toast.error("Could not update this section", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  const chosen = new Set(cmc.deliverables.map((d) => d.doc_type_key));
  const depth = (code: string) => code.split(".").length - 1;

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-border bg-card p-4">
        <h3 className="mb-3 text-sm font-semibold text-foreground">What are you writing?</h3>
        {types === null ? <StageSkeleton lines={3} /> : (
          <div className="grid gap-2 sm:grid-cols-2">
            {types.map((type) => (
              <div key={type.key}
                   className={cn("rounded-lg border p-3",
                                 chosen.has(type.key) ? "border-brand bg-brand/5"
                                   : type.unbuilt ? "border-border opacity-60" : "border-border")}>
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-foreground">{type.name}</div>
                    <div className="text-xs text-muted-foreground">
                      {type.structure_basis}
                      {type.section_count > 0 && ` · ${type.section_count} sections`}
                    </div>
                  </div>
                  {chosen.has(type.key) ? (
                    <span className="shrink-0 text-xs font-medium text-brand">Added</span>
                  ) : type.unbuilt ? (
                    <span className="shrink-0 text-xs text-muted-foreground"
                          title="Listed so you can see what is coming, rather than hidden.">
                      milestone {type.unbuilt}
                    </span>
                  ) : (
                    <Button size="sm" variant="outline" disabled={busy !== null}
                            onClick={() => add(type.key)}>
                      <Plus className="mr-1 h-3.5 w-3.5" /> Add
                    </Button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {cmc.deliverables.length > 0 && (
        <div className="rounded-xl border border-border bg-card">
          <div className="flex gap-1 border-b border-border px-2">
            {cmc.deliverables.map((d) => (
              <button
                key={d.id}
                onClick={() => setOpen(d.id)}
                className={cn(
                  "border-b-2 px-3 py-2 text-xs font-medium transition-colors",
                  open === d.id ? "border-brand text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground",
                )}
              >
                {d.doc_type_key.replace("ctd_", "").toUpperCase()}
              </button>
            ))}
          </div>
          {open && (sections[open] ?? []).length === 0 ? (
            <div className="p-4"><StageSkeleton lines={4} /></div>
          ) : (
            <ul className="max-h-[26rem] divide-y divide-border/60 overflow-y-auto">
              {(sections[open ?? ""] ?? []).map((section) => (
                <li key={section.id}
                    className={cn("flex items-center justify-between gap-3 px-4 py-1.5 text-sm",
                                  !section.enabled && "opacity-50")}>
                  <div className="flex min-w-0 items-baseline gap-2"
                       style={{ paddingLeft: `${depth(section.section_code) * 14}px` }}>
                    <span className="font-mono text-xs text-muted-foreground">{section.section_code}</span>
                    <span className={cn("truncate", section.is_container && "font-semibold")}>
                      {section.title}
                    </span>
                    {section.table_key && (
                      <span className="shrink-0 rounded bg-brand/10 px-1.5 text-[0.6rem] font-medium text-brand"
                            title="This section renders a table from verified data, not prose.">
                        <ListTree className="mr-0.5 inline h-2.5 w-2.5" />data
                      </span>
                    )}
                  </div>
                  {!section.is_container && (
                    <div className="flex shrink-0 items-center gap-2">
                      <select
                        className="h-7 rounded-md border border-input bg-transparent px-1.5 text-xs"
                        value={section.applicability}
                        disabled={busy !== null}
                        onChange={(e) => {
                          const next = e.target.value;
                          if (next === "applicable") {
                            patch(section, { applicability: next, applicability_justification: null });
                          } else {
                            setJustifying({ id: section.id, applicability: next });
                            setJustification(section.applicability_justification ?? "");
                          }
                        }}
                      >
                        {Object.entries(APPLICABILITY_LABEL).map(([value, label]) => (
                          <option key={value} value={value}>{label}</option>
                        ))}
                      </select>
                      <button
                        onClick={() => patch(section, { enabled: !section.enabled })}
                        disabled={busy !== null}
                        className={cn("rounded-full border px-2 py-0.5 text-xs font-medium",
                                      section.enabled
                                        ? "border-success/40 bg-success/10 text-success"
                                        : "border-border text-muted-foreground")}
                      >
                        {section.enabled ? "Included" : "Excluded"}
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {justifying && (
        <div className="space-y-2 rounded-xl border border-warning/40 bg-warning/10 p-4">
          <p className="text-sm font-medium text-foreground">
            Why does this section not apply?
          </p>
          <p className="text-xs text-muted-foreground">
            "Not applicable" with no reason is what an assessor sends back, so the justification
            is recorded with the section rather than remembered separately.
          </p>
          <Textarea rows={2} value={justification} onChange={(e) => setJustification(e.target.value)}
                    placeholder="No novel excipients are used in this formulation." />
          <div className="flex justify-end gap-2">
            <Button size="sm" variant="ghost" onClick={() => setJustifying(null)}>Cancel</Button>
            <Button size="sm" disabled={!justification.trim()}
                    onClick={() => {
                      const section = (sections[open ?? ""] ?? []).find((s) => s.id === justifying.id);
                      if (section) {
                        patch(section, {
                          applicability: justifying.applicability,
                          applicability_justification: justification.trim(),
                        });
                      }
                    }}>
              Save
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ sites */

function Sites({ cmc, onChanged }: { cmc: CmcProject; onChanged: () => void }) {
  const [form, setForm] = useState({ name: "", address: "", identifier: "" });
  const [busy, setBusy] = useState(false);

  async function add() {
    if (!form.name.trim()) {
      toast.error("A site needs a name.");
      return;
    }
    setBusy(true);
    try {
      await api.cmcCreateSite(cmc.id, {
        name: form.name.trim(),
        address: form.address.trim() || null,
        identifier: form.identifier.trim() || null,
      });
      setForm({ name: "", address: "", identifier: "" });
      onChanged();
    } catch (e: any) {
      toast.error("Could not add this site", { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        Named once here and referenced by every section that lists them, so 3.2.S.2.1 and
        3.2.P.3.1 cannot spell the same address two ways.
      </p>
      <div className="grid gap-2 rounded-xl border border-border bg-card p-4 sm:grid-cols-[1fr_1fr_10rem_auto]">
        <Input placeholder="Site name" value={form.name}
               onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <Input placeholder="Address" value={form.address}
               onChange={(e) => setForm({ ...form, address: e.target.value })} />
        <Input placeholder="FEI / DUNS" value={form.identifier}
               onChange={(e) => setForm({ ...form, identifier: e.target.value })} />
        <Button onClick={add} disabled={busy}>
          <Plus className="mr-1 h-4 w-4" /> Add
        </Button>
      </div>
      {cmc.sites.length === 0 ? (
        <p className="text-sm text-muted-foreground">No sites recorded yet.</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Site</th>
                <th className="px-3 py-2 font-medium">Address</th>
                <th className="px-3 py-2 font-medium">Identifier</th>
                <th className="px-3 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {cmc.sites.map((site) => (
                <tr key={site.id} className="border-b border-border/60 last:border-0">
                  <td className="px-3 py-2 font-medium text-foreground">{site.name}</td>
                  <td className="px-3 py-2 text-muted-foreground">{site.address ?? "—"}</td>
                  <td className="px-3 py-2 font-mono text-xs">{site.identifier ?? "—"}</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      onClick={async () => {
                        await api.cmcDeleteSite(site.id).catch(() => undefined);
                        onChanged();
                      }}
                      className="rounded p-1.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                      title="Remove"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
