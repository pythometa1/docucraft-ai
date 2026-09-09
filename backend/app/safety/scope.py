"""Which cases a report is allowed to count, and over which window.

This is the only place in the module that turns a report's three dates into a
filter, and it exists at M1 -- before there is a single case to filter -- for
one reason: every figure in a periodic safety report is a count, and the way
counts go wrong is that two code paths compute them slightly differently. A
tabulation and the sentence above it, a line listing and its total, this
report's cumulative figure and the last one's. Once two paths exist, the
disagreement is a number on a page that no reviewer can check without redoing
the query by hand.

So: one module, three predicates, and everything that counts asks here.

**The data lock point is a hard edge.** A report is prepared from the safety
database as it stood on one day, and nothing learned afterwards belongs in it.
`dlp()` compares the case's most recent receipt date against the lock, so a
case updated after the lock is excluded ENTIRELY rather than contributing its
newer data.

That is a deliberate choice between two imperfect ones, and it is worth being
explicit about. The store holds one row per case, carrying the latest version;
it cannot reconstruct what that case looked like on the lock date. Including
the case would put post-lock information into a report that must not contain
any -- silently, since the figure would look exactly like a figure. Excluding
it can make a cumulative count DROP between two consecutive reports, which is
wrong too but is *visible*: §11's fourth blocker is precisely "this report's
cumulative must be at least the previous report's". Between a wrong number
nobody can see and a wrong number that raises a blocker, this module takes the
one that raises a blocker. Case-level versioning at ingestion (M2) is what
removes the choice.

**A case with no receipt date is counted nowhere.** Not in the interval, not
in the cumulative, not quietly in one of them. `undated()` exists so those
cases can be shown and fixed rather than silently dropped -- a case the store
holds and no figure includes is exactly the kind of gap a reader cannot see.

**Interval membership is the INITIAL receipt.** A case first received two
intervals ago and updated in this one is follow-up on an old case, not a new
case in this interval; counting it as new would inflate every interval figure
and make the interval and cumulative columns disagree about the same case.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, func, or_, select

from app.models import PvCase, PvCaseEvent
from app.safety import registry

#: The date a case is placed in time by. Coalesced so that a store that only
#: ever recorded one of the two still filters correctly.
_RECEIPT = func.coalesce(PvCase.initial_receipt_date, PvCase.latest_receipt_date)
#: The date the lock is compared against: the most recent version of the case.
_VERSION = func.coalesce(PvCase.latest_receipt_date, PvCase.initial_receipt_date)


@dataclass(frozen=True)
class Scope:
    """One report's window, as the three dates that define it.

    Built by `scope_for` from a `PvReportInstance`; constructed directly only
    in tests, which is why the fields are plain dates rather than the row.
    """

    pv_product_id: str
    org_id: str
    period_start: date
    period_end: date
    data_lock_point: date
    #: Where cumulative counting starts: the product's IBD or DIBD, whichever
    #: this report type uses. None when the product has not recorded it, which
    #: makes cumulative figures unavailable rather than wrong.
    cumulative_from: date | None
    anchor: str

    @property
    def has_cumulative(self) -> bool:
        return self.cumulative_from is not None


def scope_for(product, report) -> Scope:
    """The window for one report instance of one product.

    The anchor comes from the registry rather than from this function, because
    which birth date a report counts from is a property of the report TYPE --
    a PBRER counts from first approval, a DSUR from first trial authorisation,
    and the two are usually years apart.
    """
    anchor = registry.cumulative_anchor(report.doc_type_key)
    return Scope(
        pv_product_id=report.pv_product_id,
        org_id=report.org_id,
        period_start=report.period_start,
        period_end=report.period_end,
        data_lock_point=report.data_lock_point,
        cumulative_from=getattr(product, anchor, None),
        anchor=anchor,
    )


# ------------------------------------------------------------- the predicates

def belongs_to(scope: Scope):
    """Rows of this product, in this tenant. Every other predicate assumes it."""
    return and_(PvCase.org_id == scope.org_id,
                PvCase.pv_product_id == scope.pv_product_id)


def dlp(scope: Scope):
    """Not learned after the lock.

    A case with no date at all fails this: it cannot be shown to predate the
    lock, and a case that cannot be placed in time is not admitted to a figure
    on the assumption that it would have been.
    """
    return and_(_VERSION.is_not(None), _VERSION <= scope.data_lock_point)


def interval(scope: Scope):
    """Received during this reporting interval, and locked."""
    return and_(belongs_to(scope), dlp(scope),
                _RECEIPT.is_not(None),
                _RECEIPT >= scope.period_start,
                _RECEIPT <= scope.period_end)


def cumulative(scope: Scope):
    """Received at any time from the birth date to the lock.

    Refuses rather than approximates when the product records no birth date:
    a cumulative figure counted from an unknown start is an interval figure
    wearing the wrong label.
    """
    if not scope.has_cumulative:
        raise CumulativeUnavailable(
            f"this product records no {scope.anchor.upper()}, so there is no date for "
            "cumulative figures to count from")
    return and_(belongs_to(scope), dlp(scope),
                _RECEIPT.is_not(None),
                _RECEIPT >= scope.cumulative_from)


def undated(scope: Scope):
    """In the store, placed in no window. Shown, never counted."""
    return and_(belongs_to(scope), _RECEIPT.is_(None))


def after_lock(scope: Scope):
    """In the store, excluded by the lock. Shown for the same reason."""
    return and_(belongs_to(scope),
                _VERSION.is_not(None),
                _VERSION > scope.data_lock_point)


class CumulativeUnavailable(Exception):
    """Asked for a cumulative figure with no date to count from."""


# ----------------------------------------------------------------- the counts

def count_cases(db, scope: Scope, predicate) -> int:
    return db.scalar(
        select(func.count(PvCase.id)).where(predicate)) or 0


def count_events(db, scope: Scope, predicate) -> int:
    """Events belonging to the cases the predicate admits.

    Expressed as a subquery on the case rather than by joining, so that one
    definition of "in scope" serves both counts -- a join with its own
    conditions is the second code path this module exists to prevent.
    """
    return db.scalar(
        select(func.count(PvCaseEvent.id)).where(
            PvCaseEvent.org_id == scope.org_id,
            PvCaseEvent.case_id.in_(select(PvCase.id).where(predicate)))) or 0


def preview(db, scope: Scope, *, baseline=None) -> dict:
    """What screen S2 shows before anybody commits to a report instance:
    how many cases this window would actually contain.

    `new_since_baseline` counts cases received after the previous approved
    report's lock -- the ones this report is the first to see. Where there is
    no baseline it is the interval count, because a first report is new in its
    entirety.
    """
    interval_cases = count_cases(db, scope, interval(scope))
    result = {
        "interval_cases": interval_cases,
        "interval_events": count_events(db, scope, interval(scope)),
        "cumulative_cases": None,
        "cumulative_events": None,
        "cumulative_from": scope.cumulative_from.isoformat() if scope.cumulative_from else None,
        "cumulative_anchor": scope.anchor,
        "new_since_baseline": interval_cases,
        "excluded_after_lock": count_cases(db, scope, after_lock(scope)),
        "undated": count_cases(db, scope, undated(scope)),
    }
    if scope.has_cumulative:
        result["cumulative_cases"] = count_cases(db, scope, cumulative(scope))
        result["cumulative_events"] = count_events(db, scope, cumulative(scope))
    else:
        result["cumulative_unavailable"] = (
            f"this product records no {scope.anchor.upper()}, so cumulative figures "
            "cannot be counted")

    if baseline is not None:
        result["new_since_baseline"] = count_cases(db, scope, and_(
            interval(scope), _RECEIPT > baseline.data_lock_point))
        result["baseline_report_id"] = baseline.id
    return result


def scope_of_case(scope: Scope, case) -> str:
    """Which window one case falls in, for a grid chip.

    Deliberately the same three questions in the same order the SQL asks them,
    so a row's badge and its presence in a figure cannot disagree.
    """
    receipt = case.initial_receipt_date or case.latest_receipt_date
    version = case.latest_receipt_date or case.initial_receipt_date
    if receipt is None:
        return "undated"
    if version is not None and version > scope.data_lock_point:
        return "after_lock"
    if scope.period_start <= receipt <= scope.period_end:
        return "interval"
    if scope.has_cumulative and receipt >= scope.cumulative_from:
        return "cumulative"
    return "outside"
