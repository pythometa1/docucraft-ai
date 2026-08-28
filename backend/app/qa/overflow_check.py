"""Will the value that resolved actually fit where the template put it?

§17 lists "Value overflows approved PDF region -> Block or route to exception
workflow". The failure it names is real and specific: an approved PDF template
addresses its fields by bounding box, so a 90-character job title written into a
box sized for 40 characters is clipped, and the letter that reaches the employee
is missing the end of their own job title with nothing in the QA record to say
so.

Two honest statements about what this module is.

It does not implement the PDF half. There is no PDF region model in this build
-- no bounding boxes, no `pdf_parser`, no anchor kind of "region bounding box"
in any compiled manifest -- so there is nothing to measure a value against.
Writing an approximation of it would be worse than leaving it out: a check that
reports "fits" because it had no geometry is indistinguishable from a check that
measured and agreed.

What it does implement is the part of the same question that is computable for a
DOCX today, which is two things:

  * A declared `max_len` overflowing. §6's FIELD object carries
    `validation: { "max_len": 80, ... }`. That is an exact, unit-free
    constraint: the manifest asserted a bound, and the resolved value either
    honours it or does not. No estimation is involved and this is blocking by
    default. Nothing in this build's compiler emits `validation` yet, so the
    check is silent until a manifest declares one -- which is the correct kind
    of silence: no constraint declared, no constraint to violate.

  * Text that will not fit a table cell whose width the template *declares*.
    A table cell with an explicit `w:tcW` in dxa is a real, approved region --
    the closest thing a DOCX has to a PDF bounding box -- and a remuneration
    table is exactly where an over-long value does damage. This one estimates,
    because deciding it exactly needs font metrics and a layout engine, so it is
    opt-in via `qa_policy` rather than on by default, and it is deliberately
    tuned to miss marginal cases rather than to invent them.

What the cell check cannot see, stated plainly, because a QA check whose limits
are undocumented gets trusted for things it never did:

  * Cells with `w:type="auto"` or `"pct"` widths, and tables with no declared
    widths at all. Skipped entirely -- their width is decided at layout time.
  * Vertical overflow. A cell that wraps to five lines and pushes the signature
    block onto another page is a real defect this does not detect.
  * Font metrics. Character advances are approximated from a mean proportional
    advance, not read from the embedded font, and style inheritance is not
    resolved -- a run whose size comes from a paragraph or table style is
    measured at `DEFAULT_FONT_HALF_POINTS`.
  * Headers, footers and text boxes outside the body element it is given.
"""

import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.qa.policy import (
    VALUE_EXCEEDS_MAX_LEN,
    VALUE_OVERFLOWS_CELL,
    QaFinding,
    QaPolicy,
    findings_for,
)
from app.templates.parsers.docx_prescan import W_NS

#: OOXML measures widths in dxa -- twentieths of a point -- so one point is 20
#: and one inch is 1440.
DXA_PER_POINT = 20

#: Word's default cell margin when neither the cell nor the table declares one.
DEFAULT_CELL_MARGIN_DXA = 108

#: 11pt, expressed in half-points as `w:sz` is. Word's default body size, and
#: what a run whose size comes from a style is measured at.
DEFAULT_FONT_HALF_POINTS = 22

# Mean advance width as a fraction of the em, by character class. These are
# approximations of a proportional Latin face, chosen low on purpose: the check
# should under-report rather than accuse a correct document. A run of capitals
# in a condensed column is the case it will miss.
_ADVANCE_WIDE = 1.0      # East Asian wide and fullwidth forms occupy a full em
_ADVANCE_SPACE = 0.25
_ADVANCE_DEFAULT = 0.5


def _q(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


@dataclass(frozen=True)
class LengthOverflow:
    field_id: str
    declared_max_len: int
    length: int
    value: str


@dataclass(frozen=True)
class CellOverflow:
    text: str
    declared_width_dxa: int
    usable_width_dxa: int
    estimated_width_dxa: int
    #: "unbreakable_token" -- one word alone is wider than the cell, so wrapping
    #: cannot save it. "no_wrap" -- the cell forbids wrapping, so the whole line
    #: has to fit on one line and does not.
    reason: str


def _advance_em(ch: str) -> float:
    if unicodedata.combining(ch):
        return 0.0  # a diacritic is drawn over the character before it
    if ch == " ":
        return _ADVANCE_SPACE
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return _ADVANCE_WIDE
    return _ADVANCE_DEFAULT


def estimated_width_dxa(text: str, font_half_points: int = DEFAULT_FONT_HALF_POINTS) -> int:
    """Roughly how wide `text` renders, in dxa. An estimate, and named as one."""
    em_dxa = font_half_points / 2 * DXA_PER_POINT
    return int(sum(_advance_em(ch) for ch in text) * em_dxa)


def declared_max_len(field: Mapping) -> int | None:
    """The field's declared length bound, or None when it declares none.

    §6 puts it at `validation.max_len`; a flat `max_len` is accepted because
    that is the shape a manifest written before the envelope settled uses. A
    value that is present but is not a positive integer raises rather than being
    ignored -- a bound nobody can parse is a bound nobody is enforcing, and the
    manifest should not be able to look as though it declared one.
    """
    validation = field.get("validation") or {}
    declared = validation.get("max_len", field.get("max_len"))
    if declared is None:
        return None
    if isinstance(declared, bool) or not isinstance(declared, int) or declared <= 0:
        raise ValueError(
            f"Field {field.get('id')!r} declares max_len={declared!r}; expected a positive integer."
        )
    return declared


def max_len_overflows(fields: Iterable[Mapping], values: Mapping) -> list:
    """Fields whose resolved value is longer than the manifest allows.

    A field with no value cannot overflow -- that absence is `missing_policy`'s
    business, and reporting it twice would put two notes on one defect.
    """
    overflows = []
    for f in fields:
        limit = declared_max_len(f)
        if limit is None:
            continue
        value = values.get(f["id"])
        if value is None:
            continue
        rendered = str(value)
        if len(rendered) > limit:
            overflows.append(
                LengthOverflow(field_id=f["id"], declared_max_len=limit, length=len(rendered), value=rendered)
            )
    return overflows


def _dxa_width(tc_pr) -> int | None:
    """A cell's declared width in dxa, or None when it is not declared in dxa."""
    if tc_pr is None:
        return None
    tc_w = tc_pr.find(_q("tcW"))
    if tc_w is None:
        return None
    # `w:type` defaults to dxa when absent. Anything else -- auto, pct, nil --
    # is decided at layout time and is not a width this check can use.
    if (tc_w.get(_q("type")) or "dxa") != "dxa":
        return None
    try:
        width = int(tc_w.get(_q("w")) or "")
    except ValueError:
        return None
    return width if width > 0 else None


def _margin_dxa(el, side: str) -> int | None:
    if el is None:
        return None
    side_el = el.find(_q(side))
    if side_el is None:
        return None
    try:
        return int(side_el.get(_q("w")) or "")
    except ValueError:
        return None


def _cell_margins(tc, tc_pr) -> tuple[int, int]:
    """(left, right) cell padding, from the cell, then the table, then Word's default."""
    cell_mar = tc_pr.find(_q("tcMar")) if tc_pr is not None else None
    table_mar = None
    parent = tc.getparent()
    while parent is not None:
        if parent.tag == _q("tbl"):
            tbl_pr = parent.find(_q("tblPr"))
            table_mar = tbl_pr.find(_q("tblCellMar")) if tbl_pr is not None else None
            break
        parent = parent.getparent()

    def pick(side: str) -> int:
        for source in (cell_mar, table_mar):
            found = _margin_dxa(source, side)
            if found is not None:
                return found
        return DEFAULT_CELL_MARGIN_DXA

    return pick("left"), pick("right")


def _no_wrap(tc_pr) -> bool:
    if tc_pr is None:
        return False
    el = tc_pr.find(_q("noWrap"))
    if el is None:
        return False
    return (el.get(_q("val")) or "true").lower() not in ("false", "0")


def _paragraph_font_half_points(p_el, default: int) -> int:
    """The largest run size on the line, since the widest glyphs decide the fit."""
    sizes = []
    for sz in p_el.iter(_q("sz")):
        try:
            value = int(sz.get(_q("val")) or "")
        except ValueError:
            continue
        if value > 0:
            sizes.append(value)
    return max(sizes) if sizes else default


def cell_overflows(body_el, default_font_half_points: int = DEFAULT_FONT_HALF_POINTS) -> list:
    """Text in the rendered body that will not fit the cell it was written into.

    Only two situations are reported, because only two are decidable without
    laying the page out: a cell that forbids wrapping and whose line is too long,
    and a single unbreakable word wider than the whole cell. Ordinary text that
    merely wraps onto another line inside the cell is not an overflow.
    """
    overflows = []
    for tc in body_el.iter(_q("tc")):
        tc_pr = tc.find(_q("tcPr"))
        declared = _dxa_width(tc_pr)
        if declared is None:
            continue
        left, right = _cell_margins(tc, tc_pr)
        usable = declared - left - right
        if usable <= 0:
            continue  # a cell with no room at all is a template defect, not an overflow
        no_wrap = _no_wrap(tc_pr)

        for p_el in tc.iter(_q("p")):
            text = "".join(t.text or "" for t in p_el.iter(_q("t")))
            if not text.strip():
                continue
            size = _paragraph_font_half_points(p_el, default_font_half_points)
            if no_wrap:
                estimate = estimated_width_dxa(text, size)
                if estimate > usable:
                    overflows.append(CellOverflow(text, declared, usable, estimate, "no_wrap"))
                    continue
            longest = max(text.split(), key=len, default="")
            estimate = estimated_width_dxa(longest, size)
            if estimate > usable:
                overflows.append(CellOverflow(longest, declared, usable, estimate, "unbreakable_token"))
    return overflows


def max_len_failures(fields: Iterable[Mapping], values: Mapping) -> list:
    return [
        f"Value for field '{o.field_id}' is {o.length} characters, which exceeds the declared "
        f"max_len of {o.declared_max_len}: {o.value[:60]!r}"
        for o in max_len_overflows(fields, values)
    ]


def cell_fit_failures(body_el, default_font_half_points: int = DEFAULT_FONT_HALF_POINTS) -> list:
    return [
        f"Text does not fit its table cell ({o.reason}): {o.estimated_width_dxa} twips estimated "
        f"against {o.usable_width_dxa} usable of a declared {o.declared_width_dxa}: {o.text[:60]!r}"
        for o in cell_overflows(body_el, default_font_half_points)
    ]


def findings(body_el, fields: Iterable[Mapping], values: Mapping, policy: QaPolicy) -> list[QaFinding]:
    """The DOCX-computable half of §17's overflow row.

    The two checks are separate names in the registry on purpose: one is an
    exact violation of a declared bound and the other is an estimate, and an
    organisation must be able to accept the estimate as a warning without
    softening the bound.
    """
    out: list[QaFinding] = []
    if policy.runs(VALUE_EXCEEDS_MAX_LEN):
        out += findings_for(VALUE_EXCEEDS_MAX_LEN, max_len_failures(fields, values), policy)
    if policy.runs(VALUE_OVERFLOWS_CELL):
        out += findings_for(VALUE_OVERFLOWS_CELL, cell_fit_failures(body_el), policy)
    return out
