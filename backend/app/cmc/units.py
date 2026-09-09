"""Reconciling two units, for the length of one comparison and no longer.

`app.cmc.values` keeps a reported value exactly as its source wrote it, and
`app.cmc.tables` prints that string. This module is the one narrow exception
to "the number is never transformed": a result written in ppm and a limit
written in % are the same kind of quantity, and refusing to compare them
would be as wrong as comparing their bare magnitudes -- which is what the
module did before, reporting a residual solvent at 0.5 % (5000 ppm) as
conforming to a limit of NMT 3000 ppm.

Three rules keep the exception from eating the discipline it sits inside:

* **The converted number never leaves the verdict.** It is not stored, not
  rendered, not written back over `value_text`, and no caller can obtain one
  except by asking for a comparison. `cmc_results.value_text` is untouched.

* **Every ratio is an exact power of ten**, held as a `Decimal` and applied by
  multiplication. `%` to `ppm` is 10^4 exactly; there is no rounding step and
  no float anywhere, so a conversion cannot introduce the fourth-decimal-place
  disagreement `values.py` exists to prevent. This also means the table is
  deliberately small: unit pairs that need a real measured constant -- a
  density to get from % v/v to ppm, a molecular weight to get from mg to mmol
  -- are ABSENT, and absent means "not comparable" rather than "close enough".

* **Unknown means unknown.** Two units that are not in one dimension here do
  not fall back to comparing magnitudes; `ratio` returns None and the caller
  must answer UNKNOWN. The only pair that compares without a table entry is a
  unit against itself, which needs no conversion to be meaningful -- so
  `cfu/g` against `cfu/g` works and `cfu/g` against `cfu/mL` does not.

Why `% w/w` converts and `% v/v` does not: a mass fraction and ppm are the
same dimension, so 1 % w/w is 10,000 ppm on any material. A volume fraction is
not, and turning it into a mass fraction needs the density of both phases. A
specification that states one and a certificate that reports the other is a
question for a person.
"""

from decimal import Decimal, InvalidOperation

#: Each dimension maps a unit to its magnitude in that dimension's base unit.
#: Base units are named in the comment, not in the data -- the base is simply
#: whichever unit has a factor of 1, and nothing outside this module needs to
#: know which one that is.
#:
#: Keys are already normalised (see `normalise`): lower case, no spaces, `µ`
#: and `μ` folded to `u`, `mcg` folded to `ug`.
_E = Decimal(10)

_DIMENSIONS: tuple[dict[str, Decimal], ...] = (
    # Mass fraction. Base: ppm.
    {
        "%": _E ** 4,
        "%w/w": _E ** 4,
        "g/100g": _E ** 4,
        "mg/g": _E ** 3,
        "ppm": Decimal(1),
        "mg/kg": Decimal(1),
        "ug/g": Decimal(1),
        "ppb": _E ** -3,
        "ug/kg": _E ** -3,
        "ng/g": _E ** -3,
    },
    # Mass. Base: mg.
    {
        "kg": _E ** 6,
        "g": _E ** 3,
        "mg": Decimal(1),
        "ug": _E ** -3,
        "ng": _E ** -6,
        "pg": _E ** -9,
    },
    # Volume. Base: mL.
    {
        "l": _E ** 3,
        "dl": _E ** 2,
        "ml": Decimal(1),
        "cc": Decimal(1),
        "ul": _E ** -3,
    },
    # Mass concentration. Base: mg/mL.
    {
        "g/ml": _E ** 3,
        "g/l": Decimal(1),
        "mg/ml": Decimal(1),
        "mg/dl": _E ** -2,
        "mg/l": _E ** -3,
        "ug/ml": _E ** -3,
        "ug/l": _E ** -6,
        "ng/ml": _E ** -6,
    },
)


def normalise(unit: str | None) -> str:
    """One spelling for a unit a source may write several ways.

    Case, spacing and the two Unicode mus (`µ` MICRO SIGN and `μ` GREEK SMALL
    LETTER MU, which look identical and are different characters) all vary
    between a specification typed in Word and a LIMS export. None of that
    changes what the unit means, so none of it should change whether two
    values can be compared.
    """
    text = (unit or "").strip().lower()
    if not text:
        return ""
    for micro in ("µ", "μ"):  # MICRO SIGN, GREEK SMALL LETTER MU
        text = text.replace(micro, "u")
    text = text.replace(" ", "").replace(" ", "").rstrip(".")
    if text.startswith("mcg"):  # a pharmacy spelling of the same unit
        text = "ug" + text[3:]
    return text


def _dimension_of(unit: str) -> dict[str, Decimal] | None:
    for dimension in _DIMENSIONS:
        if unit in dimension:
            return dimension
    return None


def ratio(frm: str | None, to: str | None) -> Decimal | None:
    """The exact multiplier taking a magnitude in `frm` to one in `to`.

    None when the two are not exactly convertible -- different dimensions, or
    a unit this module does not know. A unit against itself is 1 whether or
    not it appears in any table.
    """
    left, right = normalise(frm), normalise(to)
    if not left or not right:
        return None
    if left == right:
        return Decimal(1)
    dimension = _dimension_of(left)
    if dimension is None or right not in dimension:
        return None
    return dimension[left] / dimension[right]


def convert(value: Decimal | None, frm: str | None, to: str | None) -> Decimal | None:
    """`value` expressed in `to`, or None when that cannot be done exactly."""
    if value is None:
        return None
    factor = ratio(frm, to)
    if factor is None:
        return None
    return _plain(value * factor)


def _plain(value: Decimal) -> Decimal:
    """The same quantity, written the way a reader would write it.

    Multiplying by a power of ten leaves both a trailing-zero tail
    (`Decimal("0.500")`) and, after `normalize()`, an exponent form
    (`Decimal("5E+3")`). Neither is wrong and both are unreadable in a verdict
    a quality reviewer has to check by eye, so the exponent is expanded back
    into digits. Nothing here changes the quantity -- and none of it touches
    `value_text`, which is a different string entirely.
    """
    normalised = value.normalize()
    _sign, _digits, exponent = normalised.as_tuple()
    if isinstance(exponent, int) and exponent > 0:
        try:
            return normalised.quantize(Decimal(1))
        except InvalidOperation:  # absurdly large; the exponent form will do
            return normalised
    return normalised


def comparable(frm: str | None, to: str | None) -> bool:
    """Whether a verdict may be reached at all for this pair of units."""
    return ratio(frm, to) is not None
