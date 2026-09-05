/**
 * Describe the business -> the AI drafts the template -> type the values ->
 * a numbered invoice, in four steps.
 *
 * What each step trusts is deliberate. Step 1 trusts nothing the model says:
 * the template it produces is a draft blueprint the person can open in the
 * same studio every template uses, and publishing it runs the same lint gate.
 * Step 3's totals are a floating-point *preview*; the server recomputes every
 * figure in Decimal and its answer is the one the document prints. And the
 * invoice number is never shown before generation, because it does not exist
 * until the transaction that stores the document allocates it.
 */

import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft, ArrowRight, Check, Download, FileText, PenLine, ReceiptText,
  Sparkles, Wand2,
} from "lucide-react";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import type { Blueprint, Customer } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ErrorBanner } from "@/components/error-banner";
import { StageSkeleton } from "@/components/skeletons";
import { FadeIn, SwapIn } from "@/components/motion";
import {
  FieldValueForm, lineItemKeys, previewTotals, tableRowSpec, typedFields,
  type ManifestFieldLike,
} from "@/components/field-value-form";
import { CustomerDialog } from "./_app.invoices";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/_app/invoices_/new")({
  head: () => ({
    meta: [
      { title: "New Invoice — DocuMind AI" },
      { name: "description", content: "Describe your business, let the AI draft the template, type the values." },
    ],
  }),
  component: NewInvoicePage,
});

const STEPS = ["Template", "Details", "Line items", "Review"] as const;

type ManifestData = {
  id: string;
  fields: ManifestFieldLike[];
  blocks: Record<string, unknown>[];
};

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

function money(n: number, currency: string): string {
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(n);
  } catch {
    return `${currency} ${n.toFixed(2)}`;
  }
}

function NewInvoicePage() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [projectId, setProjectId] = useState<string | null>(null);

  // Step 1 state
  const [manifest, setManifest] = useState<ManifestData | null>(null);
  const [blueprintName, setBlueprintName] = useState<string | null>(null);

  // Step 2 state
  const [customerId, setCustomerId] = useState<string | null>(null);
  const [oneOff, setOneOff] = useState({ name: "", address: "", tax_id: "" });
  const [customers, setCustomers] = useState<Customer[] | null>(null);
  const [addingCustomer, setAddingCustomer] = useState(false);
  const [issueDate, setIssueDate] = useState(todayISO());
  const [dueDate, setDueDate] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [taxRate, setTaxRate] = useState("");
  const [taxSplit, setTaxSplit] = useState(false);

  // Step 3 state
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [rows, setRows] = useState<Record<string, unknown>[]>([{}]);

  // Step 4 state
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.generateInvoice>> | null>(null);

  useEffect(() => {
    let live = true;
    api.invoiceWorkspace()
      .then((ws) => { if (live) setProjectId(ws.project_id); })
      .catch((e) => { if (live) toast.error("Could not open the invoice workspace", { description: e?.message }); });
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
        project_id: projectId ?? undefined,
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
    } catch (e: any) {
      toast.error("The invoice could not be generated", { description: e?.message ?? String(e) });
    }
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6 p-6">
      <FadeIn>
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold text-foreground">New invoice</h1>
            <p className="text-sm text-muted-foreground">
              {blueprintName ? `Template: ${blueprintName}` : "Start from a description or a saved template."}
            </p>
          </div>
          <Button variant="ghost" asChild>
            <Link to="/invoices"><ArrowLeft className="mr-1.5 h-4 w-4" /> All invoices</Link>
          </Button>
        </div>
      </FadeIn>

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
                Pick from the client book, or add someone new — their details fill the template's bill-to section.
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
                  <option value="">— One-off (type details as fields) —</option>
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
          <SuccessPanel result={result} onAnother={() => {
            setStep(1);
            setRows([{}]);
            setResult(null);
          }} onDone={() => navigate({ to: "/invoices" })} />
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
  projectId: string | null;
  onReady: (manifest: ManifestData, blueprintName: string) => void;
}) {
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ title: string; detail: string } | null>(null);
  const [generated, setGenerated] = useState<{ blueprint: Blueprint & { generation?: { source: string; notes: string[] } } } | null>(null);
  const [existing, setExisting] = useState<Blueprint[] | null>(null);

  useEffect(() => {
    let live = true;
    api.listBlueprints(projectId ?? undefined)
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
        description, service: "invoice", project_id: projectId ?? undefined,
      });
      setGenerated({ blueprint });
      if (blueprint.generation?.source === "kit_fallback") {
        toast.info("A shipped invoice template is standing in", {
          description: blueprint.generation.notes[0],
        });
      }
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
                  <PenLine className="mr-1.5 h-3.5 w-3.5" /> Tweak in studio
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
      const a = document.createElement("a");
      a.href = url;
      a.download = `${result.number}.${format}`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
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
          {result.qa_passed
            ? "Generated, checked, and stored."
            : "Generated, but its checks found problems — it is stored as a draft."}
        </p>
      </div>
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
