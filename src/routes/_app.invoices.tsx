/**
 * The invoice registry and the client book, side by side.
 *
 * Two tabs because they are two halves of one job: the invoices the
 * organisation has issued (numbered, snapshotted, immutable-by-void rather
 * than delete), and the customers those invoices bill. Everything money is
 * rendered from the server's own strings -- this screen does no arithmetic.
 */

import { createFileRoute, Link } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { Ban, Download, FileText, Pencil, Plus, ReceiptText, Trash2, Users } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import type { Customer, InvoiceSummary } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { ErrorBanner } from "@/components/error-banner";
import { PolishedEmpty, TableSkeleton } from "@/components/skeletons";
import { FadeIn, SwapIn } from "@/components/motion";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/_app/invoices")({
  head: () => ({
    meta: [
      { title: "Invoices — DocuMind AI" },
      { name: "description", content: "Numbered invoices and the client book behind them." },
    ],
  }),
  component: InvoicesPage,
});

/* ------------------------------------------------------------------ shared */

function money(value: string | null, currency: string): string {
  if (value === null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return value;
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

async function saveBlob(url: string, filename: string) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/* ------------------------------------------------------------------ page */

function InvoicesPage() {
  const [tab, setTab] = useState<"invoices" | "customers">("invoices");

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-6">
      <FadeIn>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold text-foreground">Invoices</h1>
            <p className="text-sm text-muted-foreground">
              Numbered by the workspace, computed on the server, delivered as DOCX or PDF.
            </p>
          </div>
          <Button asChild>
            <Link to="/invoices/new">
              <Plus className="mr-1.5 h-4 w-4" /> New invoice
            </Link>
          </Button>
        </div>
      </FadeIn>

      <div className="flex gap-1 border-b border-border">
        {([["invoices", "Invoices", ReceiptText], ["customers", "Customers", Users]] as const).map(
          ([key, title, Icon]) => (
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
          ),
        )}
      </div>

      <SwapIn k={tab}>
        {tab === "invoices" ? <InvoiceList /> : <CustomerBook />}
      </SwapIn>
    </div>
  );
}

/* ------------------------------------------------------------------ invoices */

function InvoiceList() {
  const [items, setItems] = useState<InvoiceSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await api.listInvoices();
      setItems(res.items);
    } catch (e: any) {
      setError(e?.message ?? String(e));
      setItems([]);
    }
  }, []);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const res = await api.listInvoices();
        if (live) setItems(res.items);
      } catch (e: any) {
        if (live) {
          setError(e?.message ?? String(e));
          setItems([]);
        }
      }
    })();
    return () => { live = false; };
  }, []);

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
        title="No invoices yet"
        subtitle="Describe your business, let the AI draft your template, type the line items — the first one takes about a minute."
        action={<Button asChild><Link to="/invoices/new">Create your first invoice</Link></Button>}
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

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await api.listCustomers();
      setItems(res.items);
    } catch (e: any) {
      setError(e?.message ?? String(e));
      setItems([]);
    }
  }, []);

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
      <div className="flex justify-end">
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
