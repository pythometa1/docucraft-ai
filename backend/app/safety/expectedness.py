"""Whether an event is listed in the reference safety information -- as a
suggestion, never as an answer.

Expectedness decides whether a case is an expedited report, which column of a
tabulation it falls in, and whether a signal exists. It is a regulatory
determination, and §2's first principle puts it in a human's hands with no
silent defaults.

So this module computes a suggestion and writes it to
`pv_case_events.suggested_by_system_json`. It never writes
`pv_case_events.expectedness`, and there is no code path here that could: the
functions return a `Suggestion` and the caller stores it in the suggestion
column. The confirmed column is written by one endpoint, which requires the
qualified-person role.

**The basis travels with the suggestion.** A chip reading "Unlisted" asks
somebody to trust a verdict; one reading "Unlisted — no matching PT in CCDS
v3.2" asks them to check one. The second is the only useful kind, because the
person confirming is accountable for the determination and cannot be
accountable for reasoning they cannot see.

**A qualified listing is not a listing.** The RSI says "hepatic failure --
serious only", or "listed in the oncology indication". A term matched against
such an entry is NOT suggested as listed; it is suggested as needing a person,
with the condition quoted. Flattening a qualified entry to a boolean is how an
unlisted-in-this-context event ends up in the listed column.
"""

from dataclasses import dataclass, field

from sqlalchemy import select

LISTED = "listed"
UNLISTED = "unlisted"
NOT_ASSESSED = "not_assessed"

#: What a suggestion can say. `needs_person` is not a fourth expectedness
#: value -- it is the absence of a suggestion, with a reason.
SUGGESTIBLE = (LISTED, UNLISTED)


@dataclass
class Suggestion:
    """A proposal about one event, and the reasoning behind it."""

    value: str | None
    basis: str
    rsi_version_id: str | None = None
    rsi_label: str | None = None
    #: The listed entries that matched, with whatever conditions they carry.
    matched: list = field(default_factory=list)

    def as_json(self) -> dict:
        """The shape stored in `suggested_by_system_json`.

        Keyed by field, so a later milestone suggesting seriousness or
        causality adds a key rather than reshaping what is already there.
        """
        return {"expectedness": {
            "value": self.value, "basis": self.basis,
            "rsi_version_id": self.rsi_version_id, "rsi_label": self.rsi_label,
            "matched": list(self.matched),
        }}


def listed_terms(db, rsi_version_id: str) -> dict:
    """The RSI's listed terms, by folded preferred term."""
    from app.models import PvRsiListedTerm

    out: dict = {}
    for term in db.scalars(select(PvRsiListedTerm).where(
            PvRsiListedTerm.rsi_version_id == rsi_version_id)).all():
        out.setdefault(_fold(term.meddra_pt), []).append(term)
    return out


def _fold(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def suggest(event, *, terms: dict, rsi_version=None) -> Suggestion:
    """What the reference safety information says about this event's term.

    Four answers, and three of them are refusals:

    * the event is not coded -> no suggestion. Listedness is decided on the
      preferred term, and an uncoded event has none. Matching the verbatim
      text instead would compare a reporter's phrasing against a controlled
      vocabulary.
    * no RSI is pinned -> no suggestion. "Expected" has no meaning without a
      version to be expected against.
    * the term matches an entry that carries a condition -> no suggestion, the
      condition quoted. A qualified listing flattened to a boolean is how an
      event that is unlisted in this context reaches the listed column.
    * otherwise -> listed or unlisted, with the basis.
    """
    label = _rsi_label(rsi_version)
    version_id = getattr(rsi_version, "id", None)

    if rsi_version is None:
        return Suggestion(value=None, basis=(
            "no reference safety information is pinned to this report, so there is "
            "nothing for the event to be expected against"))

    pt = getattr(event, "meddra_pt", None)
    if not (pt or "").strip():
        return Suggestion(
            value=None, rsi_version_id=version_id, rsi_label=label,
            basis=("this event has no MedDRA preferred term, and listedness is "
                   "decided on the preferred term; code it first"))

    matches = terms.get(_fold(pt), [])
    if not matches:
        return Suggestion(
            value=UNLISTED, rsi_version_id=version_id, rsi_label=label,
            basis=f"no matching preferred term in {label}")

    conditioned = [m for m in matches if (m.condition_text or "").strip()]
    if conditioned:
        quoted = "; ".join(sorted({m.condition_text.strip() for m in conditioned}))
        return Suggestion(
            value=None, rsi_version_id=version_id, rsi_label=label,
            matched=[_matched(m) for m in matches],
            basis=(f"{pt} is listed in {label} but only under a condition "
                   f"({quoted}); whether this case meets it is a judgment"))

    return Suggestion(
        value=LISTED, rsi_version_id=version_id, rsi_label=label,
        matched=[_matched(m) for m in matches],
        basis=f"{pt} is listed in {label} without qualification")


def _matched(term) -> dict:
    return {"meddra_pt": term.meddra_pt, "meddra_soc": term.meddra_soc,
            "condition_text": term.condition_text}


def _rsi_label(rsi_version) -> str:
    if rsi_version is None:
        return "the reference safety information"
    return (f"{(rsi_version.rsi_type or '').upper()} "
            f"{rsi_version.version_label}").strip()


def stale_determinations(db, report) -> list:
    """Events whose confirmed expectedness was made against a different RSI
    version than this report pins.

    §11's fifth blocker, and §2's sixth principle: changing the RSI mid-cycle
    leaves determinations that were about the previous version. They are not
    re-pointed automatically -- an expectedness confirmed against CCDS 3.1 was a
    judgment about 3.1, and re-labelling it 3.2 would be recording a decision
    nobody made.
    """
    from app.models import PvCaseEvent

    if not report.rsi_version_id:
        return []
    return list(db.scalars(select(PvCaseEvent).where(
        PvCaseEvent.pv_product_id == report.pv_product_id,
        PvCaseEvent.expectedness != NOT_ASSESSED,
        PvCaseEvent.expectedness_rsi_version_id.is_not(None),
        PvCaseEvent.expectedness_rsi_version_id != report.rsi_version_id)).all())
