"""Derived figures for a clinical document's repeating table.

The clinical mirror of `app.finance.invoicing`, with one deliberate difference
in strictness. The invoice math raises on anything unparseable, because every
figure it emits lands on somebody's tax return. Here a column's total simply
does not fill unless EVERY row carries a parseable number for it: a partial
sum on a study report is a wrong number that looks deliberate, which is worse
than the honest blank the template's `on_missing` policy already handles. The
whole document is never blocked on one blank cell in an optional column.

Decimal, never float -- same reasoning as the invoice module: a float that
"looks right" for one dataset is how two totals disagree by one subject.
"""

from decimal import Decimal, InvalidOperation

#: Column types whose values are summable. `currency` is included for
#: completeness (a health-economics table is still a clinical table), though
#: the shipped kits only type counts.
NUMERIC_TYPES = ("number", "currency")


def derive_row_totals(rows: list, columns: list) -> dict:
    """`{"row_count": n, "total_<source_key>": "<sum>"}` for the repeat table.

    `columns` is the manifest TABLE_ROW's own column list, so a model-authored
    template that names its keys differently still gets its totals -- the keys
    come from the template, not from this module's imagination.
    """
    clean_rows = [r for r in rows or () if isinstance(r, dict)]
    out: dict = {"row_count": len(clean_rows)}
    for column in columns or ():
        if str((column or {}).get("type") or "") not in NUMERIC_TYPES:
            continue
        key = str(column.get("source_key") or "").strip()
        if not key:
            continue
        total = Decimal("0")
        complete = bool(clean_rows)
        for row in clean_rows:
            raw = row.get(key)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                complete = False
                break
            try:
                value = Decimal(str(raw).strip())
            except (InvalidOperation, ValueError):
                complete = False
                break
            # NaN and Infinity *construct* without raising, and "Total
            # enrolled: NaN" is exactly the confident-looking wrong number
            # this module exists to refuse. Commas are refused the same way:
            # "1,5" is 1.5 in half the world and 15 in the other, and a guess
            # off by 10x is worse than the honest blank.
            if not value.is_finite():
                complete = False
                break
            total += value
        if complete:
            # format(..., "f"), never str(normalize()): Decimal("120").normalize()
            # prints 1.2E+2, and a total in scientific notation on a study
            # report is a defect the invoice service already met once.
            out[f"total_{key}"] = format(total.normalize(), "f")
    return out
