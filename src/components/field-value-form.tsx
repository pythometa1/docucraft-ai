/**
 * A form generated from a manifest: one input per field the person actually
 * has to type, plus a repeating grid for a §6 TABLE_ROW's line items.
 *
 * Manifest-generic by construction -- nothing in here knows the word
 * "invoice". What it knows is the manifest vocabulary: fields carry a
 * `type` (string | currency | date | number | percent) that picks the input,
 * and a TABLE_ROW block carries `columns` whose `source_key`s are the keys of
 * each row the server fills from. The invoice wizard is the first consumer;
 * the next service's flow reuses this untouched.
 *
 * The totals strip at the bottom is a *preview*, computed in floating point
 * for display while the person types. The server recomputes every figure in
 * Decimal and its answer is the one the document prints -- which is why this
 * component never sends its own arithmetic anywhere.
 */

import { useMemo } from "react";
import { Plus, Trash2 } from "lucide-react";

import type { TableRowSpec } from "@/lib/types";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

/** The keys the invoice endpoint computes server-side; a form should not ask
 *  for what the server is going to overwrite anyway. */
export const SERVER_COMPUTED_FIELD_IDS = new Set([
  "invoice_number", "invoice_no", "invoice_date", "due_date", "currency",
  "customer_name", "customer_email", "customer_phone", "customer_address",
  "customer_tax_id", "customer_gstin",
  "subtotal", "tax_rate", "tax_amount", "cgst_amount", "sgst_amount",
  "grand_total", "total", "total_due",
]);

export type ManifestFieldLike = {
  id: string;
  type?: string;
  value_type?: string;
  on_missing?: string;
  [key: string]: unknown;
};

/** The first TABLE_ROW declaration in a manifest's blocks, if any. */
export function tableRowSpec(blocks: unknown[] | undefined): TableRowSpec | null {
  for (const block of (blocks ?? []) as Record<string, unknown>[]) {
    if (String(block?.object_type ?? "").toUpperCase() === "TABLE_ROW") {
      return block as unknown as TableRowSpec;
    }
  }
  return null;
}

/** The scalar fields a person types: everything the server does not compute
 *  and the repeating row does not own. */
export function typedFields(
  fields: ManifestFieldLike[] | undefined,
  spec: TableRowSpec | null,
): ManifestFieldLike[] {
  const rowOwned = new Set((spec?.columns ?? []).map((c) => c.field_id));
  return (fields ?? []).filter(
    (f) => f.id && !SERVER_COMPUTED_FIELD_IDS.has(f.id) && !rowOwned.has(f.id),
  );
}

const number = (value: unknown): number | null => {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};

/** Display-only totals while typing; the server's Decimal math is the record. */
export function previewTotals(
  rows: Record<string, unknown>[],
  taxRate: number | null,
): { subtotal: number; tax: number; total: number } | null {
  let subtotal = 0;
  for (const row of rows) {
    const amount =
      number(row.amount) ??
      (number(row.quantity) !== null && number(row.unit_price) !== null
        ? (number(row.quantity) as number) * (number(row.unit_price) as number)
        : null);
    if (amount === null) return null; // an unfinished row has no honest total
    subtotal += amount;
  }
  const tax = taxRate ? (subtotal * taxRate) / 100 : 0;
  return { subtotal, tax, total: subtotal + tax };
}

function label(id: string): string {
  return id.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

const LONG_TEXT_RE = /(address|terms|details|notes|instructions|description)$/;

function ScalarInput({ field, value, onChange }: {
  field: ManifestFieldLike;
  value: unknown;
  onChange: (v: string) => void;
}) {
  const kind = String(field.type ?? field.value_type ?? "string");
  const common = {
    id: `field-${field.id}`,
    value: value === null || value === undefined ? "" : String(value),
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
      onChange(e.target.value),
  };
  if (kind === "date") return <Input type="date" {...common} />;
  if (kind === "currency" || kind === "number" || kind === "percent") {
    return <Input type="number" step="any" inputMode="decimal" {...common} />;
  }
  if (LONG_TEXT_RE.test(field.id)) return <Textarea rows={2} {...common} />;
  return <Input type="text" {...common} />;
}

export function FieldValueForm({ fields, spec, values, onValues, rows, onRows }: {
  fields: ManifestFieldLike[];
  spec: TableRowSpec | null;
  values: Record<string, unknown>;
  onValues: (next: Record<string, unknown>) => void;
  rows: Record<string, unknown>[];
  onRows: (next: Record<string, unknown>[]) => void;
}) {
  const scalars = useMemo(() => typedFields(fields, spec), [fields, spec]);

  return (
    <div className="space-y-6">
      {scalars.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2">
          {scalars.map((field) => (
            <div key={field.id} className={cn(LONG_TEXT_RE.test(field.id) && "sm:col-span-2")}>
              <label htmlFor={`field-${field.id}`}
                     className="mb-1.5 block text-sm font-medium text-foreground">
                {label(field.id)}
                {field.on_missing === "BLOCK" && (
                  <span className="ml-1 text-destructive" title="Required — the document blocks without it">*</span>
                )}
              </label>
              <ScalarInput
                field={field}
                value={values[field.id]}
                onChange={(v) => onValues({ ...values, [field.id]: v })}
              />
            </div>
          ))}
        </div>
      )}

      {spec && (
        <LineItemsGrid spec={spec} rows={rows} onRows={onRows} />
      )}
    </div>
  );
}

function LineItemsGrid({ spec, rows, onRows }: {
  spec: TableRowSpec;
  rows: Record<string, unknown>[];
  onRows: (next: Record<string, unknown>[]) => void;
}) {
  const columns = spec.columns ?? [];

  const setCell = (rowIndex: number, key: string, value: string) => {
    onRows(rows.map((row, i) => (i === rowIndex ? { ...row, [key]: value } : row)));
  };

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-foreground">{label(spec.iterate_over)}</h3>
        <button
          type="button"
          onClick={() => onRows([...rows, {}])}
          className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs font-medium text-foreground transition-colors hover:bg-accent"
        >
          <Plus className="h-3.5 w-3.5" /> Add row
        </button>
      </div>
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/40 text-left">
              {columns.map((column) => (
                <th key={column.field_id} className="px-3 py-2 font-medium text-muted-foreground">
                  {label(column.source_key)}
                </th>
              ))}
              <th className="w-10 px-2 py-2" aria-label="Remove row" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex} className="border-b border-border/60 last:border-0">
                {columns.map((column) => {
                  const numeric = column.type === "currency" || column.type === "number";
                  return (
                    <td key={column.field_id} className="px-2 py-1.5">
                      <Input
                        type={numeric ? "number" : "text"}
                        step={numeric ? "any" : undefined}
                        inputMode={numeric ? "decimal" : undefined}
                        aria-label={`Row ${rowIndex + 1} ${label(column.source_key)}`}
                        className="h-8"
                        value={row[column.source_key] === undefined ? "" : String(row[column.source_key])}
                        onChange={(e) => setCell(rowIndex, column.source_key, e.target.value)}
                      />
                    </td>
                  );
                })}
                <td className="px-2 py-1.5 text-center">
                  <button
                    type="button"
                    onClick={() => onRows(rows.filter((_, i) => i !== rowIndex))}
                    disabled={rows.length <= 1}
                    className="rounded p-1 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive disabled:cursor-not-allowed disabled:opacity-40"
                    title={rows.length <= 1 ? "An invoice needs at least one line" : "Remove this row"}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
