"""What the finance vertical declares to the registry (`app.verticals`).

A leaf module on purpose: nothing from `app.*` is imported here, because the
registry that imports this file is itself imported by the compiler and the
routers. The money math, the router and the models live beside this file;
this file is only the declarations.
"""

from pathlib import Path

KIT_DIR = Path(__file__).parent / "kits"
KIT_IDS = ("invoice", "invoice_gst", "invoice_intl")

PROMPT_PACKS = {
    "invoice": """This template is an INVOICE. It must contain, in a sensible order:
  - a heading, and the business's own identity: <Business Name>, <Business Address>,
    plus tax registration when the description implies one (<Business GSTIN> for
    Indian GST businesses, <Business Tax ID> elsewhere)
  - invoice metadata: <Invoice Number>, <Invoice Date>, and <Due Date> when
    payment terms exist
  - a bill-to section: <Customer Name>, <Customer Address>, and the customer's
    tax id when relevant
  - ONE line-item table: a static header row, then the repeat row with
    placeholders such as <Item Description>, <Quantity>, <Unit Price>, <Amount>
    (line_items_key: line_items)
  - totals after the table: <Subtotal>, tax (<Tax Rate>, <Tax Amount> -- or
    <CGST Amount> and <SGST Amount> for Indian GST), and <Grand Total>,
    all typed currency except the rate
  - payment terms or instructions.
Currency conventions come from the description (GST and rupees for India)."""
}

FALLBACK_KITS = {"invoice": "invoice"}

NUMBER_PREFIXES = {"invoice": "INV-"}
