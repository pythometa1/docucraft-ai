/**
 * The invoice service, living where the user already works: inside a project.
 *
 * There is no separate "Invoices" section in the portal. A project whose
 * document type is Invoice renders this instead of the generic four-stage
 * pipeline -- the same screen answers "make an invoice", "what have we
 * issued", and "who do we bill", scoped to the project the person opened.
 *
 * Everything money is rendered from the server's own strings; the totals shown
 * while typing are a floating-point preview the server re-derives in Decimal.
 * The invoice number is never shown before generation, because it does not
 * exist until the transaction that stores the document allocates it.
 */

import { useEffect, useMemo, useState } from "react";
import { Link } from "@tanstack/react-router";
import {
  ArrowLeft, ArrowRight, Ban, Check, Download, FileText, Pencil, Plus,
  ReceiptText, Sparkles, Trash2, Users, Wand2,
} from "lucide-react";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import type { Blueprint, Customer, InvoiceSummary } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { ErrorBanner } from "@/components/error-banner";
import { PolishedEmpty, StageSkeleton, TableSkeleton } from "@/components/skeletons";
import { SwapIn } from "@/components/motion";
import {
  FieldValueForm, lineItemKeys, previewTotals, tableRowSpec, typedFields,
  type ManifestFieldLike,
} from "@/components/field-value-form";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ shared */

function money(value: string | number | null, currency: string): string {
  if (value === null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(n);
  } catch {
    return `${currency} ${value}`;
  }
}

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

const STATUS_TONE: Record<string, string> = {
  issued: "bg-success/15 text-success border-success/30",
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

type StudioProject = { id: string; name: string };

export function InvoiceStudio({ project }: { project: StudioProject }) {
  const [tab, setTab] = useState<"create" | "invoices" | "customers">("create");
  // Bumped when a generation lands, so the registry refetches on tab switch.
  const [generation, setGeneration] = useState(0);

  return (
    <div className="space-y-4">
      <div className="flex gap-1 border-b border-border">
        {([["create", "New invoice", Wand2],
           ["invoices", "Invoices", ReceiptText],
           ["customers", "Customers", Users]] as const).map(([key, title, Icon]) => (
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
          <InvoiceWizard
            projectId={project.id}
            onIssued={() => setGeneration((n) => n + 1)}
            onViewAll={() => setTab("invoices")}
          />
        )}
        {tab === "invoices" && <InvoiceList projectId={project.id} refreshKey={generation} />}
        {tab === "customers" && <CustomerBook />}
      </SwapIn>
    </div>
  );
}

/* ------------------------------------------------------------------ registry */

function InvoiceList({ projectId, refreshKey }: { projectId: string; refreshKey: number }) {
  const [items, setItems] = useState<InvoiceSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = async () => {
    setError(null);
    try {
      const res = await api.listInvoices({ project_id: projectId });
      setItems(res.items);
    } catch (e: any) {
      setError(e?.message ?? String(e));
      setItems([]);
    }
  };

  useEffect(() => {
    let live = true;
    api.listInvoices({ project_id: projectId })
      .then((res) => { if (live) setItems(res.items); })
      .catch((e) => { if (live) { setError(e?.message ?? String(e)); setItems([]); } });
    return () => { live = false; };
  }, [projectId, refreshKey]);

  async function download(invoice: InvoiceSummary, format: "docx" | "pdf") {
    if (!invoice.document_version_id) {
      toast.error("This invoice has no stored document.");
      return;
    }
    setBusy(`${invoice.id}:${format}`);
    try {
      const url = await api.downloadVersion(invoice.document_version_id, format);
      await saveBlob(url, `${invoice.number}.${format}`);
    } catch (e: any) {
      toast.error(format === "pdf" ? "PDF is not available" : "Download failed", {
        description: e?.message ?? String(e),
      });
    } finally {
      setBusy(null);
    }
  }

  async function voidInvoice(invoice: InvoiceSummary) {
    setBusy(`${invoice.id}:void`);
    try {
      await api.voidInvoice(invoice.id);
      toast.success(`${invoice.number} voided. Its number is kept — a numbering with silent gaps is worse.`);
      await load();
    } catch (e: any) {
      toast.error("Could not void this invoice", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  if (items === null) return <TableSkeleton rows={5} cols={6} />;
  if (error) return <ErrorBanner title="Invoices could not be loaded" message="Try again in a moment." detail={error} />;
  if (items.length === 0) {
    return (
      <PolishedEmpty
        icon={<ReceiptText className="h-8 w-8 text-muted-foreground" />}
        title="No invoices in this project yet"
        subtitle="Use the New invoice tab — describe your business, let the AI draft the template, type the line items."
      />
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border bg-muted/40 text-left text-muted-foreground">
            <th className="px-4 py-2.5 font-medium">Number</th>
            <th className="px-4 py-2.5 font-medium">Customer</th>
            <th className="px-4 py-2.5 font-medium">Total</th>
            <th className="px-4 py-2.5 font-medium">Issued</th>
            <th className="px-4 py-2.5 font-medium">Status</th>
            <th className="px-4 py-2.5 text-right font-medium">Actions</th>
          </tr>
        </thead>
        <tbody>
          {items.map((invoice) => (
            <tr key={invoice.id} className="border-b border-border/60 last:border-0 hover:bg-accent/40">
              <td className="px-4 py-2.5 font-mono text-xs font-medium text-foreground">{invoice.number}</td>
              <td className="px-4 py-2.5">{invoice.customer?.name ?? "—"}</td>
              <td className="px-4 py-2.5 tabular-nums">{money(invoice.total, invoice.currency)}</td>
              <td className="px-4 py-2.5 text-muted-foreground">{shortDate(invoice.issued_at)}</td>
              <td className="px-4 py-2.5">
                <StatusChip status={invoice.status} />
                {!invoice.qa_passed && (
                  <span className="ml-1.5 text-xs text-ai-blocked" title="This invoice failed its generation checks.">QA</span>
                )}
              </td>
              <td className="px-4 py-2.5">
                <div className="flex items-center justify-end gap-1">
                  <button
                    onClick={() => download(invoice, "docx")}
                    disabled={busy !== null || !invoice.document_version_id}
                    className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-accent disabled:opacity-40"
                    title="Download DOCX"
                  >
                    <Download className="h-4 w-4" />
                  </button>
                  <button
                    onClick={() => download(invoice, "pdf")}
                    disabled={busy !== null || !invoice.document_version_id}
                    className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-accent disabled:opacity-40"
                    title="Download PDF"
                  >
                    <FileText className="h-4 w-4" />
                  </button>
                  {invoice.status !== "void" && (
                    <button
                      onClick={() => voidInvoice(invoice)}
                      disabled={busy !== null}
                      className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive disabled:opacity-40"
                      title="Void this invoice (its number is kept)"
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

/* ------------------------------------------------------------------ customers */

const EMPTY_CUSTOMER = { name: "", email: "", phone: "", address: "", tax_id: "", default_currency: "", notes: "" };

function CustomerBook() {
  const [items, setItems] = useState<Customer[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Customer | "new" | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = async () => {
    setError(null);
    try {
      const res = await api.listCustomers();
      setItems(res.items);
    } catch (e: any) {
      setError(e?.message ?? String(e));
      setItems([]);
    }
  };

  useEffect(() => {
    let live = true;
    api.listCustomers()
      .then((res) => { if (live) setItems(res.items); })
      .catch((e) => { if (live) { setError(e?.message ?? String(e)); setItems([]); } });
    return () => { live = false; };
  }, []);

  async function remove(customer: Customer) {
    setBusy(customer.id);
    try {
      await api.deleteCustomer(customer.id);
      toast.success(`${customer.name} removed. Existing invoices keep their snapshot.`);
      await load();
    } catch (e: any) {
      toast.error("Could not remove this customer", { description: e?.message ?? String(e) });
    } finally {
      setBusy(null);
    }
  }

  if (items === null) return <TableSkeleton rows={4} cols={4} />;
  if (error) return <ErrorBanner title="Customers could not be loaded" message="Try again in a moment." detail={error} />;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          The whole workspace's client book — saved once, reused on every invoice, in any project.
        </p>
        <Button variant="outline" onClick={() => setEditing("new")}>
          <Plus className="mr-1.5 h-4 w-4" /> Add customer
        </Button>
      </div>

      {items.length === 0 ? (
        <PolishedEmpty
          icon={<Users className="h-8 w-8 text-muted-foreground" />}
          title="No customers yet"
          subtitle="Save a client once and every later invoice fills their details in a click."
          action={<Button variant="outline" onClick={() => setEditing("new")}>Add your first customer</Button>}
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/40 text-left text-muted-foreground">
                <th className="px-4 py-2.5 font-medium">Name</th>
                <th className="px-4 py-2.5 font-medium">Contact</th>
                <th className="px-4 py-2.5 font-medium">Tax ID</th>
                <th className="px-4 py-2.5 font-medium">Currency</th>
                <th className="px-4 py-2.5 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((customer) => (
                <tr key={customer.id} className="border-b border-border/60 last:border-0 hover:bg-accent/40">
                  <td className="px-4 py-2.5 font-medium text-foreground">{customer.name}</td>
                  <td className="px-4 py-2.5 text-muted-foreground">{customer.email ?? customer.phone ?? "—"}</td>
                  <td className="px-4 py-2.5 font-mono text-xs">{customer.tax_id ?? "—"}</td>
                  <td className="px-4 py-2.5">{customer.default_currency ?? "—"}</td>
                  <td className="px-4 py-2.5">
                    <div className="flex items-center justify-end gap-1">
                      <button
                        onClick={() => setEditing(customer)}
                        className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-accent"
                        title="Edit"
                      >
                        <Pencil className="h-4 w-4" />
                      </button>
                      <button
                        onClick={() => remove(customer)}
                        disabled={busy === customer.id}
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

      <CustomerDialog
        editing={editing}
        onClose={() => setEditing(null)}
        onSaved={async () => { setEditing(null); await load(); }}
      />
    </div>
  );
}

export function CustomerDialog({ editing, onClose, onSaved }: {
  editing: Customer | "new" | null;
  onClose: () => void;
  onSaved: (customer: Customer) => void | Promise<void>;
}) {
  const [form, setForm] = useState(EMPTY_CUSTOMER);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (editing === "new") setForm(EMPTY_CUSTOMER);
    else if (editing) {
      setForm({
        name: editing.name ?? "", email: editing.email ?? "", phone: editing.phone ?? "",
        address: editing.address ?? "", tax_id: editing.tax_id ?? "",
        default_currency: editing.default_currency ?? "", notes: editing.notes ?? "",
      });
    }
  }, [editing]);

  async function save() {
    if (!form.name.trim()) {
      toast.error("A customer needs a name.");
      return;
    }
    setSaving(true);
    const payload = {
      name: form.name.trim(),
      email: form.email.trim() || null,
      phone: form.phone.trim() || null,
      address: form.address.trim() || null,
      tax_id: form.tax_id.trim() || null,
      default_currency: form.default_currency.trim().toUpperCase() || null,
      notes: form.notes.trim() || null,
    };
    try {
      const saved = editing === "new" || editing === null
        ? await api.createCustomer(payload as any)
        : await api.updateCustomer(editing.id, payload as any);
      await onSaved(saved);
    } catch (e: any) {
      toast.error("Could not save this customer", { description: e?.message ?? String(e) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={editing !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{editing === "new" ? "Add a customer" : "Edit customer"}</DialogTitle>
          <DialogDescription>
            Saved once, reused on every invoice. Existing invoices keep the details they were issued with.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <Input placeholder="Name" value={form.name}
                 onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <div className="grid grid-cols-2 gap-3">
            <Input placeholder="Email" type="email" value={form.email}
                   onChange={(e) => setForm({ ...form, email: e.target.value })} />
            <Input placeholder="Phone" value={form.phone}
                   onChange={(e) => setForm({ ...form, phone: e.target.value })} />
          </div>
          <Textarea placeholder="Billing address" rows={2} value={form.address}
                    onChange={(e) => setForm({ ...form, address: e.target.value })} />
          <div className="grid grid-cols-2 gap-3">
            <Input placeholder="Tax ID / GSTIN" value={form.tax_id}
                   onChange={(e) => setForm({ ...form, tax_id: e.target.value })} />
            <Input placeholder="Currency (INR, USD…)" value={form.default_currency}
                   onChange={(e) => setForm({ ...form, default_currency: e.target.value })} />
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

const STEPS = ["Template", "Details", "Line items", "Review"] as const;

type ManifestData = {
  id: string;
  fields: ManifestFieldLike[];
  blocks: Record<string, unknown>[];
};

function InvoiceWizard({ projectId, onIssued, onViewAll }: {
  projectId: string;
  onIssued: () => void;
  onViewAll: () => void;
}) {
  const [step, setStep] = useState(0);

  const [manifest, setManifest] = useState<ManifestData | null>(null);
  const [blueprintName, setBlueprintName] = useState<string | null>(null);

  const [customerId, setCustomerId] = useState<string | null>(null);
  const [oneOff, setOneOff] = useState({ name: "", address: "", tax_id: "" });
  const [customers, setCustomers] = useState<Customer[] | null>(null);
  const [addingCustomer, setAddingCustomer] = useState(false);
  const [issueDate, setIssueDate] = useState(todayISO());
  const [dueDate, setDueDate] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [taxRate, setTaxRate] = useState("");
  const [taxSplit, setTaxSplit] = useState(false);

  const [values, setValues] = useState<Record<string, unknown>>({});
  const [rows, setRows] = useState<Record<string, unknown>[]>([{}]);

  const [result, setResult] = useState<Awaited<ReturnType<typeof api.generateInvoice>> | null>(null);

  useEffect(() => {
    let live = true;
    api.listCustomers()
      .then((res) => { if (live) setCustomers(res.items); })
      .catch(() => { if (live) setCustomers([]); });
    return () => { live = false; };
  }, []);

  const spec = useMemo(() => tableRowSpec(manifest?.blocks), [manifest]);
  const scalarFields = useMemo(() => typedFields(manifest?.fields, spec), [manifest, spec]);
  const hasGstSplit = useMemo(
    () => (manifest?.fields ?? []).some((f) => f.id === "cgst_amount"),
    [manifest],
  );

  useEffect(() => {
    // A GST template splits its tax by construction; reflect that once, when
    // the template is chosen, and leave the person free to change it.
    if (hasGstSplit) {
      setTaxSplit(true);
      setCurrency((c) => (c === "USD" ? "INR" : c));
      setTaxRate((r) => (r === "" ? "18" : r));
    }
  }, [hasGstSplit]);

  const selectedCustomer = customers?.find((c) => c.id === customerId) ?? null;
  useEffect(() => {
    if (selectedCustomer?.default_currency) setCurrency(selectedCustomer.default_currency);
  }, [selectedCustomer?.default_currency]);

  const itemKeys = useMemo(() => lineItemKeys(spec), [spec]);
  const preview = useMemo(
    () => previewTotals(rows, taxRate === "" ? null : Number(taxRate), itemKeys),
    [rows, taxRate, itemKeys],
  );

  async function generate() {
    if (!manifest) return;
    if (!customerId && !oneOff.name.trim()) {
      toast.error("An invoice bills somebody — pick a customer or type a name.");
      setStep(1);
      return;
    }
    const cleanRows = rows.filter((row) => Object.values(row).some((v) => v !== "" && v != null));
    setResult(null);
    try {
      const generated = await api.generateInvoice({
        manifest_id: manifest.id,
        project_id: projectId,
        customer_id: customerId ?? undefined,
        customer: customerId ? undefined : {
          name: oneOff.name.trim(),
          address: oneOff.address.trim() || undefined,
          tax_id: oneOff.tax_id.trim() || undefined,
        },
        line_items: cleanRows,
        fields: values,
        currency,
        tax_rate: taxRate === "" ? undefined : Number(taxRate),
        tax_split: taxSplit,
        issue_date: issueDate || undefined,
        due_date: dueDate || undefined,
        ...itemKeys,
        locale: currency === "INR" ? "en_IN" : undefined,
      });
      setResult(generated);
      setStep(4);
      onIssued();
    } catch (e: any) {
      toast.error("The invoice could not be generated", { description: e?.message ?? String(e) });
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
            onReady={(m, name) => {
              // A different template means different fields and different
              // line-item columns; values typed against the old one would ride
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
              <h2 className="text-base font-semibold text-foreground">Who is this invoice for?</h2>
              <p className="text-sm text-muted-foreground">
                {blueprintName ? `Template: ${blueprintName}. ` : ""}
                Pick from the client book, or type a one-off customer.
              </p>
            </div>
            <div className="flex flex-wrap items-end gap-3">
              <div className="min-w-56 flex-1">
                <label className="mb-1.5 block text-sm font-medium">Customer</label>
                <select
                  className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
                  value={customerId ?? ""}
                  onChange={(e) => setCustomerId(e.target.value || null)}
                >
                  <option value="">— One-off (type details below) —</option>
                  {(customers ?? []).map((customer) => (
                    <option key={customer.id} value={customer.id}>{customer.name}</option>
                  ))}
                </select>
              </div>
              <Button variant="outline" onClick={() => setAddingCustomer(true)}>New customer</Button>
            </div>
            {selectedCustomer ? (
              <div className="rounded-lg border border-border bg-muted/30 p-3 text-sm text-muted-foreground">
                <div className="font-medium text-foreground">{selectedCustomer.name}</div>
                {selectedCustomer.address && <div>{selectedCustomer.address}</div>}
                {selectedCustomer.tax_id && <div>Tax ID: {selectedCustomer.tax_id}</div>}
              </div>
            ) : (
              <div className="grid gap-3 rounded-lg border border-border bg-muted/20 p-3 sm:grid-cols-3">
                <Input placeholder="Customer name" value={oneOff.name}
                       onChange={(e) => setOneOff({ ...oneOff, name: e.target.value })} />
                <Input placeholder="Billing address" value={oneOff.address}
                       onChange={(e) => setOneOff({ ...oneOff, address: e.target.value })} />
                <Input placeholder="Tax ID / GSTIN" value={oneOff.tax_id}
                       onChange={(e) => setOneOff({ ...oneOff, tax_id: e.target.value })} />
              </div>
            )}
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <label className="mb-1.5 block text-sm font-medium">Issue date</label>
                <Input type="date" value={issueDate} onChange={(e) => setIssueDate(e.target.value)} />
              </div>
              <div>
                <label className="mb-1.5 block text-sm font-medium">Due date</label>
                <Input type="date" value={dueDate} onChange={(e) => setDueDate(e.target.value)} />
              </div>
              <div>
                <label className="mb-1.5 block text-sm font-medium">Currency</label>
                <Input value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
              </div>
              <div>
                <label className="mb-1.5 block text-sm font-medium">Tax rate (%)</label>
                <Input type="number" step="any" inputMode="decimal" value={taxRate}
                       onChange={(e) => setTaxRate(e.target.value)} placeholder="0" />
              </div>
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={taxSplit} onChange={(e) => setTaxSplit(e.target.checked)} />
              Split tax into CGST + SGST (Indian intra-state GST)
            </label>
            <StepNav onBack={() => setStep(0)} onNext={() => setStep(2)} />
          </div>
        )}

        {step === 2 && manifest && (
          <div className="space-y-5 rounded-xl border border-border bg-card p-5">
            <div>
              <h2 className="text-base font-semibold text-foreground">Fill in the values</h2>
              <p className="text-sm text-muted-foreground">
                One input per template field. The totals below are a preview — the server recomputes
                every figure before it prints.
              </p>
            </div>
            <FieldValueForm
              fields={scalarFields}
              spec={spec}
              values={values}
              onValues={setValues}
              rows={rows}
              onRows={setRows}
            />
            {preview && (
              <div className="flex justify-end">
                <div className="rounded-lg border border-border bg-muted/30 px-4 py-2 text-right text-sm tabular-nums">
                  <div className="text-muted-foreground">Subtotal {money(preview.subtotal, currency)}</div>
                  {preview.tax > 0 && <div className="text-muted-foreground">Tax {money(preview.tax, currency)}</div>}
                  <div className="font-semibold text-foreground">Total {money(preview.total, currency)}</div>
                </div>
              </div>
            )}
            <StepNav onBack={() => setStep(1)} onNext={() => setStep(3)} />
          </div>
        )}

        {step === 3 && manifest && (
          <ReviewStep
            customerName={selectedCustomer?.name ?? (oneOff.name.trim() || "— nobody yet —")}
            lineCount={rows.filter((row) => Object.values(row).some((v) => v !== "" && v != null)).length}
            preview={preview}
            currency={currency}
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

      <CustomerDialog
        editing={addingCustomer ? "new" : null}
        onClose={() => setAddingCustomer(false)}
        onSaved={async (saved) => {
          setAddingCustomer(false);
          const res = await api.listCustomers().catch(() => null);
          if (res) setCustomers(res.items);
          setCustomerId(saved.id);
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

function TemplateStep({ projectId, onReady }: {
  projectId: string;
  onReady: (manifest: ManifestData, blueprintName: string) => void;
}) {
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ title: string; detail: string } | null>(null);
  const [generated, setGenerated] = useState<{ blueprint: Blueprint & { generation?: { source: string; notes: string[] } } } | null>(null);
  const [existing, setExisting] = useState<Blueprint[] | null>(null);

  useEffect(() => {
    let live = true;
    api.listBlueprints(projectId)
      .then((res) => { if (live) setExisting(res.items); })
      .catch(() => { if (live) setExisting([]); });
    return () => { live = false; };
  }, [projectId]);

  async function describe() {
    if (!description.trim()) {
      toast.error("Describe the business first — a sentence or two is plenty.");
      return;
    }
    setBusy("describe");
    setError(null);
    try {
      const blueprint = await api.blueprintFromDescription({
        description, service: "invoice", project_id: projectId,
      });
      setGenerated({ blueprint });
      if (blueprint.generation?.source === "kit_fallback") {
        toast.info("A shipped invoice template is standing in", {
          description: blueprint.generation.notes[0],
        });
      }
      const res = await api.listBlueprints(projectId).catch(() => null);
      if (res) setExisting(res.items);
    } catch (e: any) {
      setError({ title: "The template could not be authored", detail: e?.message ?? String(e) });
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
          detail: `${e.message} — open it in the studio to fix, then come back.`,
        });
      } else {
        setError({ title: "This template could not be used", detail: e?.message ?? String(e) });
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
          <h2 className="text-base font-semibold text-foreground">Describe your business</h2>
        </div>
        <p className="mb-3 text-sm text-muted-foreground">
          What you do, where, and anything the invoice must carry — GST, payment terms, currency.
          The AI drafts a complete template you can tweak in the studio.
        </p>
        <Textarea
          rows={3}
          placeholder="A decoration company in Pune. GST-registered, invoices in rupees, payment within 14 days."
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
              <p key={i} className="text-xs text-muted-foreground">{note}</p>
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
            Templates you draft or tweak appear here for the next invoice.
          </p>
        ) : (
          <ul className="divide-y divide-border/60">
            {existing.map((blueprint) => (
              <li key={blueprint.id} className="flex items-center justify-between gap-3 py-2.5">
                <div className="flex items-center gap-2 text-sm">
                  <ReceiptText className="h-4 w-4 text-muted-foreground" />
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

function ReviewStep({ customerName, lineCount, preview, currency, onBack, onGenerate }: {
  customerName: string;
  lineCount: number;
  preview: { subtotal: number; tax: number; total: number } | null;
  currency: string;
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
          <dt className="text-muted-foreground">Billed to</dt>
          <dd className="font-medium text-foreground">{customerName}</dd>
        </div>
        <div className="rounded-lg border border-border p-3">
          <dt className="text-muted-foreground">Line items</dt>
          <dd className="font-medium text-foreground">{lineCount}</dd>
        </div>
        <div className="rounded-lg border border-border p-3">
          <dt className="text-muted-foreground">Total (preview)</dt>
          <dd className="font-medium tabular-nums text-foreground">
            {preview ? money(preview.total, currency) : "—"}
          </dd>
        </div>
      </dl>
      <div className="flex justify-between border-t border-border pt-4">
        <Button variant="ghost" onClick={onBack} disabled={busy}>
          <ArrowLeft className="mr-1.5 h-4 w-4" /> Back
        </Button>
        <Button
          onClick={async () => { setBusy(true); try { await onGenerate(); } finally { setBusy(false); } }}
          disabled={busy || lineCount === 0}
        >
          <ReceiptText className="mr-1.5 h-4 w-4" />
          {busy ? "Generating…" : "Generate invoice"}
        </Button>
      </div>
    </div>
  );
}

function SuccessPanel({ result, onAnother, onDone }: {
  result: NonNullable<Awaited<ReturnType<typeof api.generateInvoice>>>;
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
        description: e?.message ?? String(e),
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
          {result.qa_notes.slice(0, 4).map((note, i) => <li key={i}>• {note}</li>)}
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
        <button className="text-brand hover:underline" onClick={onAnother}>Another invoice with this template</button>
        <button className="text-muted-foreground hover:underline" onClick={onDone}>All invoices</button>
      </div>
    </div>
  );
}
