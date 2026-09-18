/**
 * The Safety / Pharmacovigilance module: a product safety profile, the
 * reporting intervals set up against it, and the sources those are read from.
 *
 * What this screen is really for is the three dates. A periodic safety report
 * is not a document about a product, it is a document about an INTERVAL, and
 * every figure in it is a function of the period and the data lock point. Those
 * are also the hardest thing to change once anybody has drafted against them.
 *
 * So the setup form asks the server what a proposed interval would actually
 * contain -- in the interval, cumulatively, excluded by the lock, undated --
 * before the instance exists. The counts come from `app.safety.scope`, which is
 * the same code the report itself will be built from, so the number on this
 * screen and the number in the document cannot be two different numbers.
 *
 * Sources are parsed into the case store and then STOP: de-identification runs
 * before anything is indexed, embedded or sent to a model, and that stage is
 * M3. The Sources tab says so rather than showing a green tick over half a
 * pipeline. Coding, tabulations, drafting, QC and export follow it.
 */
import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, CalendarDays, CheckCircle2, ClipboardList, Database, FileText,
  Globe, Info, ListTree, PenLine, Plus, ShieldAlert, ShieldCheck, ShieldQuestion,
  Table2, Trash2, Upload, Users,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type {
  PvApprovalStatus, PvMember, PvProduct, PvReportInstance, PvReportType,
  PvRsiVersion, PvScopePreview, PvSection,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorBanner } from "@/components/error-banner";
import { PolishedEmpty, StageSkeleton } from "@/components/skeletons";
import { SwapIn } from "@/components/motion";
import {
  SafetyCases, SafetyDeidQueue, SafetySources,
} from "@/components/safety-sources";
import { SafetyReview } from "@/components/safety-review";
import { SafetyTables } from "@/components/safety-tables";
import { SafetyEditor } from "@/components/safety-editor";
import { SafetyQc } from "@/components/safety-qc";
import { cn } from "@/lib/utils";

const SELECT_CLASS =
  "h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm";

/** Delta badges, in the words §10 uses. */
const DELTA_LABEL: Record<string, string> = {
  carried_forward: "Carried forward",
  changed: "Changed",
  new_data: "New data",
  needs_rewrite: "Needs rewrite",
  fresh: "New",
};

const DELTA_TONE: Record<string, string> = {
  carried_forward: "bg-muted text-muted-foreground",
  changed: "bg-warning/15 text-warning",
  new_data: "bg-brand/15 text-brand",
  needs_rewrite: "bg-destructive/15 text-destructive",
  fresh: "bg-muted text-muted-foreground",
};

function Disclaimer() {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-foreground">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
      <span>
        An AI-assisted drafting tool for pharmacovigilance and medical writers. It does not
        assign seriousness, expectedness or causality, does not submit to any gateway, and
        does not establish a reporting obligation. Your pharmacovigilance system of record
        governs those.
      </span>
    </div>
  );
}

export function SafetyWorkspace({ project }: { project: { id: string; name: string } }) {
  const [product, setProduct] = useState<PvProduct | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    let live = true;
    api.pvListProducts()
      .then((res) => {
        if (!live) return;
        setProduct(res.items.find((p) => p.project_id === project.id) ?? null);
      })
      .catch((e: any) => {
        if (live) { setError(e?.message ?? String(e)); setProduct(null); }
      });
    return () => { live = false; };
  }, [project.id, refresh]);

  if (product === undefined) return <StageSkeleton lines={4} />;
  if (error) {
    return <ErrorBanner title="The safety profile could not be loaded"
                        message="Try again in a moment." detail={error} />;
  }
  return product === null
    ? <SafetySetup projectId={project.id} onCreated={() => setRefresh((n) => n + 1)} />
    : <SafetyOverview product={product} onChanged={() => setRefresh((n) => n + 1)} />;
}

/* ------------------------------------------------------------------- S1 setup */

function SafetySetup({ projectId, onCreated }: {
  projectId: string; onCreated: () => void;
}) {
  const [form, setForm] = useState({
    product_name: "", inn: "", mah_name: "", atc_code: "", ibd: "", dibd: "",
  });
  const [regions, setRegions] = useState<string[]>([]);
  const [allRegions, setAllRegions] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api.pvReportTypes()
      .then((res) => { if (live) setAllRegions(res.regions); })
      .catch(() => { /* the picker degrades to no region chips */ });
    return () => { live = false; };
  }, []);

  const set = (key: keyof typeof form) => (value: string) =>
    setForm((f) => ({ ...f, [key]: value }));

  async function create() {
    if (!form.product_name.trim()) {
      toast.error("A safety profile needs a product name.");
      return;
    }
    setBusy(true);
    try {
      await api.pvCreateProduct({
        project_id: projectId,
        product_name: form.product_name.trim(),
        inn: form.inn.trim() || undefined,
        mah_name: form.mah_name.trim() || undefined,
        atc_code: form.atc_code.trim() || undefined,
        ibd: form.ibd || null,
        dibd: form.dibd || null,
        regions,
      });
      toast.success("Safety profile created.");
      onCreated();
    } catch (e: any) {
      toast.error("The safety profile could not be created",
                  { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <Disclaimer />
      <div className="rounded-2xl surface-raised p-6">
        <h2 className="text-lg font-semibold">Product safety profile</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          The two birth dates are what cumulative figures count from, and which one a
          report uses depends on its type: a PBRER counts from first approval anywhere,
          a DSUR from the first trial authorisation. They are usually years apart.
        </p>

        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <Field label="Product name" required>
            <Input value={form.product_name} onChange={(e) => set("product_name")(e.target.value)}
                   placeholder="Vigilazine 10 mg tablets" />
          </Field>
          <Field label="INN / active substance">
            <Input value={form.inn} onChange={(e) => set("inn")(e.target.value)}
                   placeholder="vigilazine" />
          </Field>
          <Field label="Marketing authorisation holder">
            <Input value={form.mah_name} onChange={(e) => set("mah_name")(e.target.value)} />
          </Field>
          <Field label="ATC code">
            <Input value={form.atc_code} onChange={(e) => set("atc_code")(e.target.value)}
                   placeholder="N05AX00" />
          </Field>
          <Field label="IBD — international birth date"
                 hint="First approval anywhere. PBRER and PADER cumulative figures count from here.">
            <Input type="date" value={form.ibd} onChange={(e) => set("ibd")(e.target.value)} />
          </Field>
          <Field label="DIBD — development birth date"
                 hint="First clinical trial authorisation. DSUR cumulative figures count from here.">
            <Input type="date" value={form.dibd} onChange={(e) => set("dibd")(e.target.value)} />
          </Field>

          <div className="sm:col-span-2">
            <label className="mb-1.5 block text-sm font-medium">Regions</label>
            <div className="flex flex-wrap gap-1.5">
              {allRegions.map((region) => {
                const on = regions.includes(region);
                return (
                  <button key={region} type="button"
                          onClick={() => setRegions((rs) =>
                            on ? rs.filter((r) => r !== region) : [...rs, region])}
                          className={cn(
                            "rounded-full border px-3 py-1 text-xs transition-colors",
                            on ? "border-brand bg-brand/10 text-brand"
                               : "border-border text-muted-foreground hover:text-foreground")}>
                    {region}
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        <div className="mt-5 flex justify-end">
          <Button onClick={create} disabled={busy}>
            {busy ? "Creating…" : "Create safety profile"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, hint, required, children }: {
  label: string; hint?: string; required?: boolean; children: React.ReactNode;
}) {
  return (
    <div>
      <label className="mb-1.5 block text-sm font-medium">
        {label}{required && <span className="text-destructive"> *</span>}
      </label>
      {children}
      {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

/* --------------------------------------------------------------- the tab shell */

type Tab = "profile" | "reports" | "sources" | "deid" | "cases" | "review"
  | "tables" | "write" | "qc" | "calendar" | "roles";

const TABS: [Tab, string, typeof ListTree][] = [
  ["profile", "Product profile", ClipboardList],
  ["reports", "Reporting intervals", FileText],
  ["sources", "Sources", Upload],
  ["deid", "De-identification", ShieldAlert],
  ["cases", "Case store", Database],
  ["review", "Case review", ShieldQuestion],
  ["tables", "Tables", Table2],
  ["write", "Write", PenLine],
  ["qc", "Checks & export", ShieldCheck],
  ["calendar", "Calendar", CalendarDays],
  ["roles", "Roles", Users],
];

function SafetyOverview({ product, onChanged }: {
  product: PvProduct; onChanged: () => void;
}) {
  const [tab, setTab] = useState<Tab>(product.reports.length ? "reports" : "profile");

  return (
    <div className="space-y-4">
      <Disclaimer />

      <div className="rounded-2xl surface-raised p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-lg font-semibold">{product.product_name}</h2>
            <p className="mt-0.5 text-sm text-muted-foreground">
              {[product.inn, product.mah_name].filter(Boolean).join(" · ") || "—"}
            </p>
          </div>
          <div className="flex flex-wrap gap-4 text-xs">
            <Anchor label="IBD" value={product.ibd}
                    note="PBRER / PADER cumulative" />
            <Anchor label="DIBD" value={product.dibd} note="DSUR cumulative" />
            <div className="text-right">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                Your role
              </div>
              <div className="font-medium">
                {(product.my_role ?? "none").replace("_", " ")}
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="flex gap-1 border-b border-border">
        {TABS.map(([key, label, Icon]) => (
          <button key={key} onClick={() => setTab(key)}
                  className={cn(
                    "inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition-colors",
                    tab === key ? "border-brand text-foreground"
                                : "border-transparent text-muted-foreground hover:text-foreground")}>
            <Icon className="h-4 w-4" /> {label}
          </button>
        ))}
      </div>

      <SwapIn k={tab}>
        {tab === "profile" && <ProfileTab product={product} onChanged={onChanged} />}
        {tab === "reports" && <ReportsTab product={product} onChanged={onChanged} />}
        {tab === "sources" && <SafetySources productId={product.id} />}
        {tab === "deid" && <SafetyDeidQueue productId={product.id} />}
        {tab === "cases" && <CasesTab product={product} />}
        {tab === "review" && (
          <SafetyReview productId={product.id} reports={product.reports} />
        )}
        {tab === "tables" && <SafetyTables reports={product.reports} />}
        {tab === "write" && <SafetyEditor reports={product.reports} />}
        {tab === "qc" && <SafetyQc reports={product.reports} />}
        {tab === "calendar" && <CalendarTab product={product} />}
        {tab === "roles" && <RolesTab product={product} onChanged={onChanged} />}
      </SwapIn>
    </div>
  );
}

function Anchor({ label, value, note }: {
  label: string; value: string | null; note: string;
}) {
  return (
    <div className="text-right">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn("font-mono font-medium", !value && "text-muted-foreground")}>
        {value ?? "not set"}
      </div>
      <div className="text-[10px] text-muted-foreground">{note}</div>
    </div>
  );
}

/* ------------------------------------------------------------------ S1 profile */

function ProfileTab({ product, onChanged }: {
  product: PvProduct; onChanged: () => void;
}) {
  const [versions, setVersions] = useState<PvRsiVersion[] | null>(null);
  const [rsiTypes, setRsiTypes] = useState<string[]>([]);
  const [approvals, setApprovals] = useState<PvApprovalStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [newVersion, setNewVersion] = useState({
    rsi_type: "ccds", version_label: "", effective_date: "",
  });

  useEffect(() => {
    let live = true;
    Promise.all([api.pvRsiVersions(product.id), api.pvApprovalStatuses(product.id)])
      .then(([rsi, approval]) => {
        if (!live) return;
        setVersions(rsi.items);
        setRsiTypes(rsi.rsi_types);
        setApprovals(approval.items);
      })
      .catch((e: any) => {
        if (live) { setError(e?.message ?? String(e)); setVersions([]); setApprovals([]); }
      });
    return () => { live = false; };
  }, [product.id, reload]);

  async function addVersion() {
    if (!newVersion.version_label.trim()) {
      toast.error("An RSI version needs a label, such as “3.2”.");
      return;
    }
    setBusy("rsi");
    try {
      await api.pvCreateRsiVersion(product.id, {
        rsi_type: newVersion.rsi_type,
        version_label: newVersion.version_label.trim(),
        effective_date: newVersion.effective_date || null,
      });
      setNewVersion({ rsi_type: newVersion.rsi_type, version_label: "", effective_date: "" });
      setReload((n) => n + 1);
      onChanged();
      toast.success("RSI version added.");
    } catch (e: any) {
      toast.error("The version could not be added", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function pin(version: PvRsiVersion) {
    setBusy(version.id);
    try {
      const result = await api.pvPinRsiVersion(version.id);
      setReload((n) => n + 1);
      onChanged();
      if (result.open_reports_on_previous_version) {
        toast.warning(
          `${version.rsi_type.toUpperCase()} ${version.version_label} is now current.`,
          { description:
              `${result.open_reports_on_previous_version} open report(s) stay pinned to the ` +
              "previous version. Expectedness confirmed against it was a determination " +
              "about that version; review each one." });
      } else {
        toast.success(`${version.rsi_type.toUpperCase()} ${version.version_label} is now current.`);
      }
    } catch (e: any) {
      toast.error("The version could not be pinned", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  if (versions === null || approvals === null) return <StageSkeleton lines={5} />;
  if (error) {
    return <ErrorBanner title="The profile could not be loaded"
                        message="Try again in a moment." detail={error} />;
  }

  return (
    <div className="space-y-5">
      <section className="rounded-xl border border-border p-4">
        <div className="flex items-center gap-2">
          <ShieldCheck className="h-4 w-4 text-brand" />
          <h3 className="font-medium">Reference safety information</h3>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          The pinned version is what “expected” means. Pinning is a qualified-person act,
          and a report keeps the version it was pinned to even after a newer one arrives —
          an expectedness confirmed against one version is a determination about that version.
        </p>

        {versions.length === 0 ? (
          <p className="mt-3 rounded-lg border border-border bg-muted/30 px-4 py-5 text-center text-sm text-muted-foreground">
            No RSI versions yet. A report cannot decide what is listed without one.
          </p>
        ) : (
          <div className="mt-3 overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                  <th className="px-3 py-2 font-medium">Type</th>
                  <th className="px-3 py-2 font-medium">Version</th>
                  <th className="px-3 py-2 font-medium">Effective</th>
                  <th className="px-3 py-2 font-medium">Listed terms</th>
                  <th className="px-3 py-2 text-right font-medium">Current</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.id} className={cn("border-b border-border/60 last:border-0",
                                               v.is_current && "bg-success/5")}>
                    <td className="px-3 py-1.5 uppercase">{v.rsi_type}</td>
                    <td className="px-3 py-1.5 font-medium text-foreground">{v.version_label}</td>
                    <td className="px-3 py-1.5 font-mono text-xs text-muted-foreground">
                      {v.effective_date ?? "—"}
                    </td>
                    <td className="px-3 py-1.5 text-muted-foreground">{v.listed_term_count ?? 0}</td>
                    <td className="px-3 py-1.5 text-right">
                      {v.is_current ? (
                        <span className="inline-flex items-center gap-1 text-xs text-success">
                          <CheckCircle2 className="h-3.5 w-3.5" /> pinned
                        </span>
                      ) : (
                        <Button variant="outline" size="sm" className="h-7 px-2 text-xs"
                                disabled={busy !== null} onClick={() => pin(v)}>
                          Pin
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="mt-3 grid gap-2 sm:grid-cols-[9rem_1fr_11rem_auto]">
          <select className={SELECT_CLASS} value={newVersion.rsi_type}
                  onChange={(e) => setNewVersion((v) => ({ ...v, rsi_type: e.target.value }))}>
            {rsiTypes.map((t) => <option key={t} value={t}>{t.toUpperCase()}</option>)}
          </select>
          <Input placeholder="Version label, e.g. 3.2" value={newVersion.version_label}
                 onChange={(e) => setNewVersion((v) => ({ ...v, version_label: e.target.value }))} />
          <Input type="date" value={newVersion.effective_date}
                 onChange={(e) => setNewVersion((v) => ({ ...v, effective_date: e.target.value }))} />
          <Button variant="outline" onClick={addVersion} disabled={busy !== null}>
            <Plus className="mr-1 h-4 w-4" /> Add
          </Button>
        </div>
      </section>

      <ApprovalStatusSection product={product} approvals={approvals}
                             onChanged={() => setReload((n) => n + 1)} />
    </div>
  );
}

function ApprovalStatusSection({ product, approvals, onChanged }: {
  product: PvProduct; approvals: PvApprovalStatus[]; onChanged: () => void;
}) {
  const [form, setForm] = useState({ country: "", approval_date: "", status: "approved" });
  const [busy, setBusy] = useState(false);

  async function add() {
    if (!form.country.trim()) {
      toast.error("A row needs a country.");
      return;
    }
    setBusy(true);
    try {
      await api.pvAddApprovalStatus(product.id, {
        country: form.country.trim(),
        approval_date: form.approval_date || null,
        status: form.status,
      });
      setForm({ country: "", approval_date: "", status: form.status });
      onChanged();
    } catch (e: any) {
      toast.error("The row could not be added", { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-xl border border-border p-4">
      <div className="flex items-center gap-2">
        <Globe className="h-4 w-4 text-brand" />
        <h3 className="font-medium">Worldwide marketing approval status</h3>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        Rendered into the report as a computed table, so it is recorded once here rather
        than retyped into every interval.
      </p>

      {approvals.length === 0 ? (
        <p className="mt-3 rounded-lg border border-border bg-muted/30 px-4 py-5 text-center text-sm text-muted-foreground">
          No countries recorded yet.
        </p>
      ) : (
        <div className="mt-3 overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Country</th>
                <th className="px-3 py-2 font-medium">Approved</th>
                <th className="px-3 py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {approvals.map((a) => (
                <tr key={a.id} className="border-b border-border/60 last:border-0">
                  <td className="px-3 py-1.5 font-medium text-foreground">{a.country}</td>
                  <td className="px-3 py-1.5 font-mono text-xs text-muted-foreground">
                    {a.approval_date ?? "—"}
                  </td>
                  <td className="px-3 py-1.5 capitalize">{a.status.replace("_", " ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="mt-3 grid gap-2 sm:grid-cols-[1fr_11rem_10rem_auto]">
        <Input placeholder="Country, e.g. DE" value={form.country}
               onChange={(e) => setForm((f) => ({ ...f, country: e.target.value }))} />
        <Input type="date" value={form.approval_date}
               onChange={(e) => setForm((f) => ({ ...f, approval_date: e.target.value }))} />
        <select className={SELECT_CLASS} value={form.status}
                onChange={(e) => setForm((f) => ({ ...f, status: e.target.value }))}>
          {["approved", "withdrawn", "not_approved", "pending"].map((s) => (
            <option key={s} value={s}>{s.replace("_", " ")}</option>
          ))}
        </select>
        <Button variant="outline" onClick={add} disabled={busy}>
          <Plus className="mr-1 h-4 w-4" /> Add
        </Button>
      </div>
    </section>
  );
}

/* --------------------------------------------------------- S2 report instances */

function ReportsTab({ product, onChanged }: {
  product: PvProduct; onChanged: () => void;
}) {
  const [types, setTypes] = useState<PvReportType[] | null>(null);
  const [sections, setSections] = useState<Record<string, PvSection[]>>({});
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.pvReportTypes()
      .then((res) => { if (live) setTypes(res.items); })
      .catch((e: any) => { if (live) toast.error("Report types could not be loaded",
                                                 { description: e?.message ?? String(e) }); });
    return () => { live = false; };
  }, []);

  async function showSections(report: PvReportInstance) {
    if (open === report.id) { setOpen(null); return; }
    setOpen(report.id);
    if (sections[report.id]) return;
    try {
      const res = await api.pvReportSections(report.id);
      setSections((s) => ({ ...s, [report.id]: res.items }));
    } catch (e: any) {
      toast.error("The section tree could not be loaded",
                  { description: e?.message ?? String(e) });
    }
  }

  async function remove(report: PvReportInstance) {
    if (!window.confirm(
      `Delete the ${report.doc_type_name} for ${report.period_start} to ` +
      `${report.period_end}? Its sections and drafts go with it. The case store does not.`
    )) return;
    setBusy(report.id);
    try {
      await api.pvDeleteReport(report.id);
      toast.success("Report instance deleted.");
      onChanged();
    } catch (e: any) {
      toast.error("The report could not be deleted", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-5">
      <NewReportForm product={product} types={types ?? []} onCreated={onChanged} />

      {product.reports.length === 0 ? (
        <PolishedEmpty
          icon={<FileText className="h-8 w-8 text-muted-foreground" />}
          title="No reporting intervals yet"
          subtitle="A safety report is a document about an interval. Set the period and the data lock point above, and the section tree is created from the report type's structure."
        />
      ) : (
        <div className="space-y-3">
          {product.reports.map((report) => (
            <div key={report.id} className="rounded-xl border border-border">
              <div className="flex flex-wrap items-start justify-between gap-3 p-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{report.doc_type_name}</span>
                    <span className="rounded-full bg-muted px-2 py-0.5 text-[0.65rem] capitalize">
                      {report.status.replace("_", " ")}
                    </span>
                  </div>
                  <p className="mt-1 font-mono text-xs text-muted-foreground">
                    {report.period_start} → {report.period_end} · lock {report.data_lock_point}
                  </p>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {report.structure_basis}
                    {report.cumulative_anchor && (
                      <> · cumulative from {report.cumulative_anchor.toUpperCase()}</>
                    )}
                    {report.baseline_report_id && <> · has a baseline</>}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <Button variant="outline" size="sm" className="h-8"
                          onClick={() => showSections(report)}>
                    <ListTree className="mr-1 h-3.5 w-3.5" />
                    {open === report.id ? "Hide" : "Sections"}
                    {report.section_count != null && ` (${report.section_count})`}
                  </Button>
                  <button onClick={() => remove(report)} disabled={busy !== null}
                          className="rounded p-1.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                          title="Delete this report instance">
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>

              {open === report.id && (
                <div className="border-t border-border px-4 py-3">
                  {!sections[report.id] ? (
                    <StageSkeleton lines={3} />
                  ) : (
                    <ul className="space-y-0.5">
                      {sections[report.id].map((s) => (
                        <li key={s.id}
                            className="flex items-center gap-2 text-sm"
                            style={{ paddingLeft: `${(s.level - 1) * 1.25}rem` }}>
                          <span className="w-16 shrink-0 font-mono text-xs text-muted-foreground">
                            {s.section_code}
                          </span>
                          <span className={cn("truncate",
                                              s.is_container && "font-medium")}>
                            {s.title}
                          </span>
                          {s.table_key && (
                            <span className="shrink-0 rounded bg-brand/10 px-1.5 text-[0.65rem] text-brand"
                                  title="This section carries a computed table; the model writes the prose around it, never the table.">
                              {s.table_key}
                            </span>
                          )}
                          <span className={cn("shrink-0 rounded px-1.5 text-[0.65rem]",
                                              DELTA_TONE[s.delta_status] ?? DELTA_TONE.fresh)}>
                            {DELTA_LABEL[s.delta_status] ?? s.delta_status}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function NewReportForm({ product, types, onCreated }: {
  product: PvProduct; types: PvReportType[]; onCreated: () => void;
}) {
  const [form, setForm] = useState({
    doc_type_key: "dsur", period_start: "", period_end: "", data_lock_point: "",
    rsi_version_id: "", baseline_report_id: "", meddra_version: "",
  });
  const [preview, setPreview] = useState<PvScopePreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const complete = Boolean(form.period_start && form.period_end && form.data_lock_point);
  const chosen = types.find((t) => t.key === form.doc_type_key);
  const baselines = useMemo(
    () => product.reports.filter((r) => r.doc_type_key === form.doc_type_key),
    [product.reports, form.doc_type_key],
  );

  // The counts come from the server, through the same scope layer the report
  // will be built from -- and they are asked for on a delay, because the dates
  // are typed a character at a time.
  useEffect(() => {
    if (!complete) { setPreview(null); setPreviewError(null); return; }
    let live = true;
    const timer = setTimeout(() => {
      api.pvScopePreview(product.id, {
        doc_type_key: form.doc_type_key,
        period_start: form.period_start,
        period_end: form.period_end,
        data_lock_point: form.data_lock_point,
        baseline_report_id: form.baseline_report_id || null,
      })
        .then((res) => { if (live) { setPreview(res); setPreviewError(null); } })
        .catch((e: any) => {
          if (live) { setPreview(null); setPreviewError(e?.message ?? String(e)); }
        });
    }, 350);
    return () => { live = false; clearTimeout(timer); };
  }, [product.id, form.doc_type_key, form.period_start, form.period_end,
      form.data_lock_point, form.baseline_report_id, complete]);

  async function create() {
    setBusy(true);
    try {
      const made = await api.pvCreateReport(product.id, {
        doc_type_key: form.doc_type_key,
        period_start: form.period_start,
        period_end: form.period_end,
        data_lock_point: form.data_lock_point,
        rsi_version_id: form.rsi_version_id || null,
        baseline_report_id: form.baseline_report_id || null,
        meddra_version: form.meddra_version.trim() || null,
      });
      toast.success(
        `${made.doc_type_name} created with ${made.sections.length} sections.`,
        made.carried_forward
          ? { description: `${made.carried_forward} section(s) carried forward from the baseline.` }
          : undefined);
      setForm((f) => ({ ...f, period_start: "", period_end: "", data_lock_point: "" }));
      setPreview(null);
      onCreated();
    } catch (e: any) {
      toast.error("The report instance could not be created",
                  { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-xl border border-border p-4">
      <h3 className="font-medium">New reporting interval</h3>
      <p className="mt-1 text-xs text-muted-foreground">
        The data lock point is absolute: nothing received after it counts towards this
        report, in either the interval or the cumulative column.
      </p>

      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <Field label="Report type">
          <select className={SELECT_CLASS} value={form.doc_type_key}
                  onChange={(e) => setForm((f) => ({
                    ...f, doc_type_key: e.target.value, baseline_report_id: "" }))}>
            {types.map((t) => <option key={t.key} value={t.key}>{t.name}</option>)}
          </select>
        </Field>
        <Field label="Period start"><Input type="date" value={form.period_start}
          onChange={(e) => setForm((f) => ({ ...f, period_start: e.target.value }))} /></Field>
        <Field label="Period end"><Input type="date" value={form.period_end}
          onChange={(e) => setForm((f) => ({ ...f, period_end: e.target.value }))} /></Field>
        <Field label="Data lock point"
               hint="On or after the period end, or the report could not count its own final weeks.">
          <Input type="date" value={form.data_lock_point}
                 onChange={(e) => setForm((f) => ({ ...f, data_lock_point: e.target.value }))} />
        </Field>
        <Field label="Reference safety information">
          <select className={SELECT_CLASS} value={form.rsi_version_id}
                  onChange={(e) => setForm((f) => ({ ...f, rsi_version_id: e.target.value }))}>
            <option value="">Not pinned</option>
            {product.rsi_versions.map((v) => (
              <option key={v.id} value={v.id}>
                {v.rsi_type.toUpperCase()} {v.version_label}{v.is_current ? " (current)" : ""}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Baseline (previous report)"
               hint="Stable sections load its text; data sections regenerate.">
          <select className={SELECT_CLASS} value={form.baseline_report_id}
                  onChange={(e) => setForm((f) => ({ ...f, baseline_report_id: e.target.value }))}>
            <option value="">None — this is the first</option>
            {baselines.map((r) => (
              <option key={r.id} value={r.id}>
                {r.period_start} → {r.period_end}
              </option>
            ))}
          </select>
        </Field>
      </div>

      {chosen && (
        <p className="mt-2 text-xs text-muted-foreground">
          {chosen.structure_basis} · {chosen.section_count} sections · cumulative figures
          count from the {chosen.cumulative_anchor.toUpperCase()}
          {product[chosen.cumulative_anchor as "ibd" | "dibd"]
            ? ` (${product[chosen.cumulative_anchor as "ibd" | "dibd"]})`
            : " — not recorded on this product, so cumulative figures will be unavailable"}
        </p>
      )}

      <ScopePreview preview={preview} error={previewError} complete={complete} />

      <div className="mt-4 flex justify-end">
        <Button onClick={create} disabled={busy || !complete || Boolean(previewError)}>
          {busy ? "Creating…" : "Create reporting interval"}
        </Button>
      </div>
    </section>
  );
}

function ScopePreview({ preview, error, complete }: {
  preview: PvScopePreview | null; error: string | null; complete: boolean;
}) {
  if (!complete) {
    return (
      <p className="mt-4 rounded-lg border border-dashed border-border px-3 py-4 text-center text-xs text-muted-foreground">
        Set the period and the data lock point to see how many cases this interval would contain.
      </p>
    );
  }
  if (error) {
    return (
      <div className="mt-4 flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
        <span>{error}</span>
      </div>
    );
  }
  if (!preview) return <p className="mt-4 text-xs text-muted-foreground">Counting…</p>;

  return (
    <div className="mt-4 rounded-lg border border-border bg-muted/20 p-3">
      <div className="grid gap-3 sm:grid-cols-4">
        <Count label="In this interval" value={preview.interval_cases}
               note={`${preview.interval_events} event(s)`} />
        <Count label="Cumulative"
               value={preview.cumulative_cases}
               note={preview.cumulative_unavailable
                 ? "no birth date recorded"
                 : `since ${preview.cumulative_from} (${preview.cumulative_anchor.toUpperCase()})`} />
        <Count label="New since baseline" value={preview.new_since_baseline}
               note="not in the previous report" />
        <Count label="Excluded by the lock" value={preview.excluded_after_lock}
               note="received or updated after it" tone={preview.excluded_after_lock ? "warn" : undefined} />
      </div>
      {(preview.undated > 0 || preview.cumulative_unavailable) && (
        <div className="mt-3 space-y-1 border-t border-border pt-2 text-xs text-muted-foreground">
          {preview.undated > 0 && (
            <p className="flex items-start gap-1.5">
              <Info className="mt-0.5 h-3 w-3 shrink-0" />
              {preview.undated} case(s) carry no receipt date and are counted in neither
              column. They are in the store and need a date before they can appear anywhere.
            </p>
          )}
          {preview.cumulative_unavailable && (
            <p className="flex items-start gap-1.5">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-warning" />
              {preview.cumulative_unavailable}. Record it on the product profile.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function Count({ label, value, note, tone }: {
  label: string; value: number | null; note: string; tone?: "warn";
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn("text-xl font-semibold",
                         value === null && "text-muted-foreground",
                         tone === "warn" && value ? "text-warning" : undefined)}>
        {value === null ? "—" : value}
      </div>
      <div className="text-[10px] text-muted-foreground">{note}</div>
    </div>
  );
}

/* ---------------------------------------------------------------- the cases */

function CasesTab({ product }: { product: PvProduct }) {
  // Which interval to badge against. A case's scope is only meaningful
  // relative to a report's three dates, so the reader chooses one -- and the
  // badge then comes from the same layer the figures will.
  const [reportId, setReportId] = useState<string>(
    product.reports.length ? product.reports[0].id : "");

  return (
    <div className="space-y-3">
      {product.reports.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-xs text-muted-foreground">
            Show each case's position in
          </label>
          <select className={cn(SELECT_CLASS, "h-8 w-auto text-xs")}
                  value={reportId} onChange={(e) => setReportId(e.target.value)}>
            <option value="">no particular interval</option>
            {product.reports.map((r) => (
              <option key={r.id} value={r.id}>
                {r.doc_type_name} · {r.period_start} → {r.period_end}
              </option>
            ))}
          </select>
        </div>
      )}
      <SafetyCases productId={product.id} reportInstanceId={reportId || undefined} />
    </div>
  );
}

/* ----------------------------------------------------------------- the calendar */

function CalendarTab({ product }: { product: PvProduct }) {
  const [data, setData] = useState<{ items: PvReportInstance[]; disclaimer: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let live = true;
    api.pvCalendar(product.id)
      .then((res) => { if (live) setData(res); })
      .catch((e: any) => { if (live) setError(e?.message ?? String(e)); });
    return () => { live = false; };
  }, [product.id, reload]);

  if (error) {
    return <ErrorBanner title="The calendar could not be loaded"
                        message="Try again in a moment." detail={error} />;
  }
  if (!data) return <StageSkeleton lines={4} />;

  return (
    <div className="space-y-4">
      {/* The disclaimer arrives with the dates rather than being written into
          this screen, so the dates cannot be rendered without it. */}
      <div className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
        <span>{data.disclaimer}</span>
      </div>

      {data.items.length === 0 ? (
        <PolishedEmpty
          icon={<CalendarDays className="h-8 w-8 text-muted-foreground" />}
          title="Nothing scheduled"
          subtitle="Reporting intervals appear here in data-lock-point order once you create them."
        />
      ) : (
        <div className="space-y-2">
          {data.items.map((report) => (
            <div key={report.id}
                 className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border px-4 py-3">
              <div className="min-w-0">
                <div className="font-medium">{report.doc_type_name}</div>
                <div className="font-mono text-xs text-muted-foreground">
                  {report.period_start} → {report.period_end}
                </div>
              </div>
              <div className="text-right text-xs">
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  Data lock point
                </div>
                <div className="font-mono font-medium">{report.data_lock_point}</div>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {(report.due_dates ?? []).length === 0 ? (
                  <span className="text-xs text-muted-foreground">no regional dates recorded</span>
                ) : (
                  (report.due_dates ?? []).map((due) => (
                    <span key={due.id}
                          className="rounded-full border border-border px-2 py-0.5 text-xs"
                          title={due.basis_note ?? undefined}>
                      {due.region} {due.submission_due_date ?? "—"}
                    </span>
                  ))
                )}
              </div>
              <AddDueDate reportId={report.id} onAdded={() => setReload((n) => n + 1)} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function AddDueDate({ reportId, onAdded }: { reportId: string; onAdded: () => void }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ region: "EU", submission_due_date: "", basis_note: "" });
  const [busy, setBusy] = useState(false);

  if (!open) {
    return (
      <Button variant="outline" size="sm" className="h-7 px-2 text-xs"
              onClick={() => setOpen(true)}>
        <Plus className="mr-1 h-3 w-3" /> Date
      </Button>
    );
  }

  async function add() {
    setBusy(true);
    try {
      await api.pvAddDueDate(reportId, {
        region: form.region,
        submission_due_date: form.submission_due_date || null,
        basis_note: form.basis_note.trim() || null,
      });
      setOpen(false);
      setForm({ region: "EU", submission_due_date: "", basis_note: "" });
      onAdded();
    } catch (e: any) {
      toast.error("The date could not be recorded", { description: e?.message ?? String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex w-full flex-wrap items-center gap-2 border-t border-border pt-2">
      <Input className="h-8 w-20 text-xs" value={form.region}
             onChange={(e) => setForm((f) => ({ ...f, region: e.target.value }))} />
      <Input className="h-8 w-36 text-xs" type="date" value={form.submission_due_date}
             onChange={(e) => setForm((f) => ({ ...f, submission_due_date: e.target.value }))} />
      <Input className="h-8 flex-1 text-xs" placeholder="Basis, e.g. 90 days from the lock"
             value={form.basis_note}
             onChange={(e) => setForm((f) => ({ ...f, basis_note: e.target.value }))} />
      <Button size="sm" className="h-8" onClick={add} disabled={busy}>Save</Button>
      <Button size="sm" variant="outline" className="h-8"
              onClick={() => setOpen(false)}>Cancel</Button>
    </div>
  );
}

/* --------------------------------------------------------------------- roles */

function RolesTab({ product, onChanged }: { product: PvProduct; onChanged: () => void }) {
  const [data, setData] = useState<{
    items: PvMember[]; roles: { key: string; label: string }[]; my_role: string | null;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let live = true;
    api.pvMembers(product.id)
      .then((res) => { if (live) setData(res); })
      .catch((e: any) => { if (live) setError(e?.message ?? String(e)); });
    return () => { live = false; };
  }, [product.id, reload]);

  async function setRole(member: PvMember, pv_role: string) {
    setBusy(member.id);
    try {
      await api.pvGrantRole(product.id, { user_id: member.user_id, pv_role });
      setReload((n) => n + 1);
      onChanged();
      toast.success(`${member.user_email ?? "That user"} is now a ${pv_role.replace("_", " ")}.`);
    } catch (e: any) {
      toast.error("The role could not be changed", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  if (error) {
    return <ErrorBanner title="Roles could not be loaded"
                        message="Try again in a moment." detail={error} />;
  }
  if (!data) return <StageSkeleton lines={4} />;

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
        Pharmacovigilance judgment is human-owned and nobody holds a role by inference.
        Only a qualified person may confirm expectedness, seriousness or causality, clear
        the de-identification gate, or sign a report off — and naming the first qualified
        person on a product needs the ability to manage users in this organisation.
      </div>

      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
              <th className="px-3 py-2 font-medium">Person</th>
              <th className="px-3 py-2 font-medium">Role</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((member) => (
              <tr key={member.id} className="border-b border-border/60 last:border-0">
                <td className="px-3 py-2">
                  <div className="font-medium text-foreground">
                    {member.user_name ?? member.user_email ?? member.user_id}
                  </div>
                  {member.user_name && member.user_email && (
                    <div className="text-xs text-muted-foreground">{member.user_email}</div>
                  )}
                </td>
                <td className="px-3 py-2">
                  <select className={cn(SELECT_CLASS, "h-8 max-w-xs text-xs")}
                          value={member.pv_role} disabled={busy !== null}
                          onChange={(e) => setRole(member, e.target.value)}>
                    {data.roles.map((r) => (
                      <option key={r.key} value={r.key}>{r.label}</option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
