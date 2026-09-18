"""The service registry: every vertical this backend ships, by explicit import.

No scanning, no entry points. A new vertical is one folder with a leaf
`service.py`, one import line here, and one entry in SERVICES. Each service.py
must import nothing from `app.*` (stdlib only) -- this module is imported BY
the compiler, the blueprint router, the kit loader and the allocator, so an
upward import from a service module would be a cycle.

What a service declares:
  KIT_DIR / KIT_IDS   the starter kits it ships (`<id>.yaml` files in its folder)
  PROMPT_PACKS        service key -> scaffolding appended to the authoring prompt
  FALLBACK_KITS       service key -> the kit that stands in when authoring cannot run
  NUMBER_PREFIXES     numbering key -> prefix ("invoice" -> "INV-")
"""

from app.clinical import service as clinical
from app.finance import service as finance
from app.hr import service as hr
from app.legal import service as legal
from app.medaff import service as medaff

#: Registry order is kit display order after "blank". This exact order
#: reproduces the pre-registry KIT_ORDER: offer, contract, clinical, medaff,
#: invoice, invoice_gst, invoice_intl.
SERVICES = (hr, legal, clinical, medaff, finance)


def _merged(attr: str) -> dict:
    """One dict from every service's `attr` fragment, refusing collisions.

    Two services claiming one key is a wiring mistake that would otherwise be
    settled silently by import order -- better a RuntimeError at startup than
    an invoice authored with a clinical prompt in production.
    """
    out: dict = {}
    for svc in SERVICES:
        fragment = getattr(svc, attr, None) or {}
        overlap = out.keys() & fragment.keys()
        if overlap:
            raise RuntimeError(
                f"duplicate {attr} keys across services: {sorted(overlap)}")
        out.update(fragment)
    return out


PROMPT_PACKS: dict = _merged("PROMPT_PACKS")
FALLBACK_KITS: dict = _merged("FALLBACK_KITS")
NUMBER_PREFIXES: dict = _merged("NUMBER_PREFIXES")

#: Kit id -> the directory holding `<id>.yaml`, in display order. Built with
#: the same refusal: a kit id belongs to exactly one service.
KIT_DIRS: dict = {}
KIT_IDS: tuple = ()
for _svc in SERVICES:
    for _kit_id in getattr(_svc, "KIT_IDS", ()):
        if _kit_id in KIT_DIRS:
            raise RuntimeError(f"kit id {_kit_id!r} declared by two services")
        KIT_DIRS[_kit_id] = _svc.KIT_DIR
        KIT_IDS += (_kit_id,)
