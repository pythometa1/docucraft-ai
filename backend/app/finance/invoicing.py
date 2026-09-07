"""Invoice money math, in Decimal, in one place.

Every figure an invoice prints is computed here and nowhere else -- not in the
frontend (whose live totals are a display convenience the server re-derives),
and not in floating point. `Decimal` with half-up rounding at two places is
what a human with a calculator produces, and "the total is off by one paisa
from what my accountant gets" is a support thread this module exists to
prevent.

Strict on purpose: a line item whose amount cannot be derived is an error, not
a zero. A zero that looks deliberate on an invoice is the worst kind of wrong
number.
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

TWO_PLACES = Decimal("0.01")


class UncomputableAmount(ValueError):
    """A line item from which no amount can be derived."""


def _as_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def money(value) -> Decimal:
    d = _as_decimal(value)
    if d is None:
        raise UncomputableAmount(f"{value!r} is not an amount")
    return d.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass
class InvoiceTotals:
    line_items: list
    subtotal: Decimal
    tax_rate: Decimal | None
    tax_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    grand_total: Decimal
    notes: list = field(default_factory=list)

    def record_values(self) -> dict:
        """The keys a template's totals fields resolve from.

        Both `total` and `grand_total` are provided, and CGST/SGST always --
        harmless when the template has no field for them, present when a GST
        template does. A key that is absent renders as a missing value; a key
        that is present and unused costs nothing.
        """
        out = {
            "subtotal": str(self.subtotal),
            "tax_amount": str(self.tax_amount),
            "cgst_amount": str(self.cgst_amount),
            "sgst_amount": str(self.sgst_amount),
            "grand_total": str(self.grand_total),
            "total": str(self.grand_total),
            "total_due": str(self.grand_total),
        }
        if self.tax_rate is not None:
            # `format(..., "f")` and not `str(normalize())`: Decimal("18")
            # normalises to 1.8E+1, and scientific notation in an audit
            # snapshot reads as corruption even where the renderer recovers.
            out["tax_rate"] = format(self.tax_rate.normalize(), "f")
        return out


def compute_totals(
    line_items: list,
    *,
    quantity_key: str = "quantity",
    unit_price_key: str = "unit_price",
    amount_key: str = "amount",
    tax_rate=None,
    tax_split: bool = False,
) -> InvoiceTotals:
    """Enrich each line with its amount, then total the invoice.

    A line's amount is the one it declares, or `quantity x unit_price` when it
    declares none. A line that yields neither raises -- the row number in the
    message, because "line 3" is what the person fixing it needs.

    `tax_split` halves the tax into CGST and SGST for Indian intra-state GST;
    the half-total is rounded once and the CGST carries the remainder, so the
    two halves always sum back to the tax exactly.
    """
    enriched: list = []
    subtotal = Decimal("0")
    for index, item in enumerate(line_items or ()):
        if not isinstance(item, dict):
            raise UncomputableAmount(
                f"line {index + 1} is {type(item).__name__}, not an object of values")
        row = dict(item)
        amount = _as_decimal(row.get(amount_key))
        if amount is None:
            quantity = _as_decimal(row.get(quantity_key))
            unit_price = _as_decimal(row.get(unit_price_key))
            if quantity is None or unit_price is None:
                raise UncomputableAmount(
                    f"line {index + 1} has no {amount_key!r}, and no "
                    f"{quantity_key!r} x {unit_price_key!r} to derive one from")
            amount = quantity * unit_price
        amount = amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        row[amount_key] = str(amount)
        subtotal += amount
        enriched.append(row)

    subtotal = subtotal.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    rate = _as_decimal(tax_rate)
    if rate is not None and rate < 0:
        raise UncomputableAmount(f"a tax rate of {rate} is not a tax rate")
    tax_amount = (
        (subtotal * rate / Decimal("100")).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        if rate is not None else Decimal("0.00")
    )

    if tax_split:
        sgst = (tax_amount / 2).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        cgst = tax_amount - sgst  # the remainder rides on CGST; the halves always re-sum
    else:
        cgst = sgst = Decimal("0.00")

    grand_total = (subtotal + tax_amount).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return InvoiceTotals(
        line_items=enriched, subtotal=subtotal, tax_rate=rate,
        tax_amount=tax_amount, cgst_amount=cgst, sgst_amount=sgst,
        grand_total=grand_total,
    )
