"""The tables a periodic safety report prints, computed and never typed.

§2's second principle: the model never counts. Every case count, tabulation
cell and exposure figure is computed here, from the confirmed case store, and a
drafted section carries a `[TABLE: key]` marker where the table belongs. The
marker is resolved at export, so a correction in the review grid reaches every
table without a single section being regenerated.

Three properties are built into every builder rather than left to each one.

**One path to a count.** Case tables filter through `app.safety.scope` -- the
interval, cumulative and lock predicates -- and through nothing else. Two code
paths that both count "serious cases in the interval" will eventually disagree
by one, and the disagreement is a number on a page nobody can check without
redoing the query.

**Every cell knows where it came from.** A numeric cell carries the ids of the
events and cases that make it up (`Tabulation.cells`), which is what the
drill-down reads and what QC reconciles a line listing against a summary with.
A count with no provenance is a count nobody can verify.

**Nothing unconfirmed is counted as confirmed.** An event whose expectedness
no qualified person has decided is neither listed nor unlisted, so it goes in a
column that says so rather than into either. Acceptance criterion 3: a
suggestion is counted nowhere until it is confirmed. The same for exposure: an
unconfirmed figure is printed and named in `.missing`, which QC blocks on.

Seriousness needs one more sentence. E2B states seriousness at the CASE level;
most imports carry no event-level flag. So an event counts as serious if a
person has confirmed it serious, and otherwise inherits its case's seriousness
-- the statement the source actually made. Reading an unset event flag as "not
serious" would move every event of every serious case into the non-serious
column.
"""

from dataclasses import dataclass, field


from sqlalchemy import select

from app.docgen import grids
from app.docgen.grids import HOLE, TableUnavailable, UnknownTable
from app.safety import scope as scope_mod
from app.safety.expectedness import LISTED, UNLISTED

__all__ = ["Tabulation", "BUILDERS", "render", "TableUnavailable", "UnknownTable",
           "HOLE", "RELATED"]

#: Causality answers that make an event a reaction. Stored as free text by the
#: source systems, so compared case-insensitively on the whole answer. An event
#: with no causality at all is not a reaction and is not quietly assumed to be
#: one -- it is counted in `.missing` instead.
RELATED = frozenset({
    "related", "possibly related", "probably related", "certainly related",
    "definitely related", "possible", "probable", "certain", "likely",
    "reasonable possibility", "yes", "y", "suspected",
})

UNCODED = "[uncoded]"


@dataclass
class Tabulation:
    """One computed table and everything a caller must know about it."""

    key: str
    title: str
    columns: list
    rows: list
    blocks: list
    #: "r{row}c{col}" -> {"events": [...ids], "cases": [...ids]} for every
    #: numeric cell. The drill-down, and the reconciliation QC runs.
    cells: dict = field(default_factory=dict)
    #: Named figures for reconciliation: a line listing's row count must equal
    #: the summary's serious-unlisted total for the same scope, and QC checks
    #: that from here rather than by re-counting.
    totals: dict = field(default_factory=dict)
    #: The window the figures describe. Carried with the table so a figure
    #: quoted in prose can be checked against the scope it came from.
    scope: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    missing: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "columns": self.columns,
                "rows": self.rows, "cells": self.cells, "totals": self.totals,
                "scope": self.scope, "notes": self.notes, "missing": self.missing}


# ------------------------------------------------------------------- helpers

def _scope_meta(scope: scope_mod.Scope) -> dict:
    return {
        "period_start": scope.period_start.isoformat(),
        "period_end": scope.period_end.isoformat(),
        "data_lock_point": scope.data_lock_point.isoformat(),
        "cumulative_from": (scope.cumulative_from.isoformat()
                            if scope.cumulative_from else None),
        "cumulative_anchor": scope.anchor,
    }


def _finish(key, title, columns, rows, *, cells=None, totals=None, scope=None,
            notes=None, missing=None) -> Tabulation:
    grids.check_rectangular(key, columns, rows)
    blocks = [grids.heading(title), grids.grid(columns, rows)]
    blocks.extend(_note(note) for note in notes or [])
    return Tabulation(key=key, title=title, columns=list(columns),
                      rows=[list(r) for r in rows], blocks=blocks,
                      cells=cells or {}, totals=totals or {},
                      scope=scope or {}, notes=list(notes or []),
                      missing=list(missing or []))


def _note(text: str) -> dict:
    from app.templates import blueprint as bp

    return bp.paragraph([bp.segment("static", text, italic=True)])


def _cases_in(db, scope, predicate):
    from app.models import PvCase

    return {c.id: c for c in db.scalars(select(PvCase).where(predicate)).all()}


def _events_for(db, case_ids):
    from app.models import PvCaseEvent

    if not case_ids:
        return []
    return list(db.scalars(select(PvCaseEvent).where(
        PvCaseEvent.case_id.in_(list(case_ids)))).all())


def is_serious(event, case) -> bool:
    """Serious if a person confirmed it serious, else as the case says.

    See the module docstring: E2B states seriousness at case level, and an
    unset event flag is an absence, not a determination.
    """
    if event.confirmed_by is not None:
        return bool(event.is_serious)
    return bool(event.is_serious) or bool(getattr(case, "is_serious", False))


def expectedness_bucket(event) -> str:
    """listed | unlisted | unconfirmed. A suggestion is never a bucket."""
    if event.confirmed_by is not None and event.expectedness in (LISTED, UNLISTED):
        return event.expectedness
    return "unconfirmed"


def is_related(event) -> bool | None:
    """True if either causality says related, False if both say otherwise, and
    None if nobody assessed it -- which is not the same as unrelated."""
    answers = [a for a in (event.causality_company, event.causality_reporter)
               if (a or "").strip()]
    if not answers:
        return None
    return any(a.strip().lower() in RELATED for a in answers)


def _add(cells, key, event, case):
    entry = cells.setdefault(key, {"events": [], "cases": []})
    entry["events"].append(event.id)
    if case.id not in entry["cases"]:
        entry["cases"].append(case.id)


# ------------------------------------------------------------- the builders

_BUCKETS = (("serious", UNLISTED), ("serious", LISTED), ("serious", "unconfirmed"),
            ("non_serious", UNLISTED), ("non_serious", LISTED),
            ("non_serious", "unconfirmed"))
_BUCKET_LABEL = {
    ("serious", UNLISTED): "Serious unlisted",
    ("serious", LISTED): "Serious listed",
    ("serious", "unconfirmed"): "Serious, expectedness not confirmed",
    ("non_serious", UNLISTED): "Non-serious unlisted",
    ("non_serious", LISTED): "Non-serious listed",
    ("non_serious", "unconfirmed"): "Non-serious, expectedness not confirmed",
}


def _soc_pt_table(db, scope, *, key, title, case_filter=None,
                  include_non_serious=True, windows=("interval", "cumulative")):
    """SOC x PT, split by seriousness and confirmed expectedness, per window.

    Interval and cumulative are separate columns and never summed: §8's sixth
    rule says the two must never merge, and a table that added them would teach
    the prose beside it to do the same.
    """
    predicates = {}
    if "interval" in windows:
        predicates["interval"] = scope_mod.interval(scope)
    if "cumulative" in windows:
        if scope.has_cumulative:
            predicates["cumulative"] = scope_mod.cumulative(scope)
        else:
            windows = tuple(w for w in windows if w != "cumulative")

    buckets = [b for b in _BUCKETS if include_non_serious or b[0] == "serious"]
    counts: dict = {}            # (soc, pt) -> {(window, bucket): [events]}
    cells_raw: dict = {}         # (soc, pt, window, bucket) -> [(event, case)]
    uncoded = {w: 0 for w in windows}
    totals: dict = {}

    for window in windows:
        cases = _cases_in(db, scope, predicates[window])
        if case_filter is not None:
            cases = {k: c for k, c in cases.items() if case_filter(c)}
        for event in _events_for(db, cases.keys()):
            case = cases[event.case_id]
            serious = is_serious(event, case)
            if not include_non_serious and not serious:
                continue
            bucket = ("serious" if serious else "non_serious",
                      expectedness_bucket(event))
            soc = event.meddra_soc or "SOC not coded"
            pt = event.meddra_pt or UNCODED
            if not event.meddra_pt:
                uncoded[window] += 1
            counts.setdefault((soc, pt), {})
            cells_raw.setdefault((soc, pt, window, bucket), []).append((event, case))
            totals[f"{window}_events"] = totals.get(f"{window}_events", 0) + 1
            name = f"{window}_{bucket[0]}_{bucket[1]}"
            totals[name] = totals.get(name, 0) + 1
        totals[f"{window}_cases"] = len(cases)

    if not counts:
        raise TableUnavailable(
            "no event falls in this report's "
            + (" or ".join(windows) or "reporting") + " window")

    columns = ["System organ class", "Preferred term"]
    for window in windows:
        for bucket in buckets:
            columns.append(f"{window.capitalize()}: {_BUCKET_LABEL[bucket]}")

    rows, cells = [], {}
    for r, (soc, pt) in enumerate(sorted(counts, key=lambda k: (k[0].lower(),
                                                                k[1].lower()))):
        row = [soc, pt]
        c = 2
        for window in windows:
            for bucket in buckets:
                contributors = cells_raw.get((soc, pt, window, bucket), [])
                row.append(str(len(contributors)))
                for event, case in contributors:
                    _add(cells, f"r{r}c{c}", event, case)
                c += 1
        rows.append(row)

    missing, notes = [], []
    for window in windows:
        unconfirmed = sum(totals.get(f"{window}_{s}_unconfirmed", 0)
                          for s in ("serious", "non_serious"))
        if unconfirmed:
            missing.append(
                f"{unconfirmed} {window} event(s) have no confirmed expectedness and "
                "are counted in the 'not confirmed' columns, not as listed or unlisted")
        if uncoded[window]:
            missing.append(
                f"{uncoded[window]} {window} event(s) carry no MedDRA preferred term "
                f"and are grouped under {UNCODED}")
    if "cumulative" not in windows and not scope.has_cumulative:
        notes.append(
            f"Cumulative figures are not shown: the product records no "
            f"{scope.anchor.upper()} to count from.")
    return _finish(key, title, columns, rows, cells=cells, totals=totals,
                   scope=_scope_meta(scope), notes=notes, missing=missing)


def summary_tab_soc_pt(db, ctx):
    return _soc_pt_table(
        db, ctx.scope, key="summary_tab_soc_pt",
        title="Summary tabulation of adverse events by system organ class and "
              "preferred term")


def summary_tab_trials(db, ctx):
    t = _soc_pt_table(
        db, ctx.scope, key="summary_tab_trials",
        title="Cumulative summary tabulation of serious adverse events from "
              "clinical trials",
        case_filter=lambda c: c.report_source == "clinical_trial",
        include_non_serious=False, windows=("cumulative",))
    t.notes.append("Treatment arm is not held in the case store; counts are pooled "
                   "across arms, and blinded-arm attribution is not shown.")
    return t


def line_listing_sar(db, ctx):
    """Serious adverse reactions in the interval, de-identified.

    A reaction is an event somebody assessed as related. A serious event with
    no causality at all is NOT listed as a reaction and not dropped silently:
    it is counted in `.missing`, because "nobody assessed it" and "unrelated"
    are different statements and a line listing that confused them would be
    short by exactly the cases nobody got round to.
    """
    scope = ctx.scope
    cases = _cases_in(db, scope, scope_mod.interval(scope))
    rows, cells, unassessed = [], {}, 0
    events = sorted(_events_for(db, cases.keys()),
                    key=lambda e: (cases[e.case_id].worldwide_case_id or "",
                                   e.meddra_pt or ""))
    for event in events:
        case = cases[event.case_id]
        if not is_serious(event, case):
            continue
        related = is_related(event)
        if related is None:
            unassessed += 1
            continue
        if not related:
            continue
        # The coded term, never the verbatim one. A reporter's own words are
        # free text, and free text is where identifiers hide.
        term = event.meddra_pt or UNCODED
        age_sex = " / ".join(x for x in (
            f"{case.patient_age:g}" if case.patient_age is not None else None,
            case.patient_sex) if x) or HOLE
        rows.append([
            grids.text(case.worldwide_case_id), grids.text(case.country_of_occurrence),
            age_sex, term, grids.text(event.onset_date.isoformat()
                                      if event.onset_date else None),
            grids.text(", ".join(case.seriousness_criteria or []).replace("_", " ")),
            grids.text(event.outcome),
            grids.text(event.causality_company or event.causality_reporter),
            grids.text(expectedness_bucket(event).replace("unconfirmed",
                                                          "not confirmed")),
        ])
        cells[f"r{len(rows) - 1}c0"] = {"events": [event.id], "cases": [case.id]}
    if not rows and not unassessed:
        raise TableUnavailable("no serious adverse reaction falls in the interval")
    columns = ["Case", "Country", "Age / sex", "Reaction (PT)", "Onset",
               "Seriousness criteria", "Outcome", "Causality", "Expectedness"]
    missing = []
    if unassessed:
        missing.append(
            f"{unassessed} serious event(s) in the interval have no causality "
            "assessment and are not listed as reactions")
    return _finish("line_listing_sar",
                   "Line listing of serious adverse reactions in the reporting "
                   "period", columns, rows, cells=cells,
                   totals={"interval_sar_rows": len(rows),
                           "interval_serious_unassessed": unassessed},
                   scope=_scope_meta(scope), missing=missing)


def exposure_table(db, ctx):
    from app.models import PvExposure

    rows_db = db.scalars(select(PvExposure).where(
        PvExposure.report_instance_id == ctx.report.id
    ).order_by(PvExposure.context, PvExposure.region)).all()
    if not rows_db:
        raise TableUnavailable("no exposure has been entered for this report")
    rows, missing = [], []
    for e in rows_db:
        rows.append([grids.text(e.context.replace("_", " ")), grids.text(e.region),
                     grids.text(e.population_descriptor),
                     grids.text(e.measure.replace("_", " ")),
                     grids.text(e.value_text), grids.text(e.calculation_method_note)])
        if not e.confirmed_by:
            missing.append(f"exposure {e.measure} ({e.region or 'all regions'}) is "
                           "not confirmed")
        if not (e.calculation_method_note or "").strip():
            missing.append(f"exposure {e.measure} ({e.region or 'all regions'}) "
                           "states no calculation method")
    return _finish("exposure_table", "Estimated exposure",
                   ["Context", "Region", "Population", "Measure", "Value", "Method"],
                   rows, totals={"exposure_rows": len(rows)},
                   scope=_scope_meta(ctx.scope), missing=missing)


def signal_overview(db, ctx):
    """New and ongoing signals, and those closed during the interval."""
    from app.models import PvSignal

    scope = ctx.scope
    signals = db.scalars(select(PvSignal).where(
        PvSignal.pv_product_id == ctx.report.pv_product_id
    ).order_by(PvSignal.detection_date)).all()
    shown = [s for s in signals
             if s.status != "closed"
             or (s.closure_date and scope.period_start <= s.closure_date
                 <= scope.data_lock_point)]
    if not shown:
        raise TableUnavailable("no signal is open or was closed in the interval")
    rows, missing = [], []
    for s in shown:
        rows.append([grids.text(s.signal_reference), grids.text(
            ", ".join(s.meddra_terms or []) or s.description),
            grids.text((s.detection_source or "").replace("_", " ")),
            grids.text(s.detection_date.isoformat() if s.detection_date else None),
            grids.text(s.status), grids.text(s.action_taken)])
        if s.status == "closed" and not ((s.conclusion or "").strip()
                                         and (s.action_taken or "").strip()):
            missing.append(f"closed signal {s.signal_reference or s.id} has no "
                           "conclusion or no action recorded")
    return _finish("signal_overview", "Overview of signals: new, ongoing or closed",
                   ["Signal", "Terms", "Source", "Detected", "Status", "Action"],
                   rows, totals={"signals_shown": len(rows)},
                   scope=_scope_meta(scope), missing=missing)


_CONCERN_ORDER = ("important_identified_risk", "important_potential_risk",
                  "missing_information")


def safety_concern_table(db, ctx):
    from app.models import PvSafetyConcern

    concerns = db.scalars(select(PvSafetyConcern).where(
        PvSafetyConcern.pv_product_id == ctx.report.pv_product_id,
        PvSafetyConcern.status == "current")).all()
    if not concerns:
        raise TableUnavailable("no current safety concern is recorded")
    concerns = sorted(concerns, key=lambda c: (
        _CONCERN_ORDER.index(c.concern_type) if c.concern_type in _CONCERN_ORDER
        else 99, c.title.lower()))
    rows = [[grids.text(c.concern_type.replace("_", " ").capitalize()),
             grids.text(c.title), grids.text(", ".join(c.meddra_terms or [])),
             grids.text(c.rmp_part_reference)] for c in concerns]
    return _finish("safety_concern_table", "Summary of safety concerns",
                   ["Category", "Safety concern", "Terms", "RMP reference"], rows,
                   totals={"concerns": len(rows)}, scope=_scope_meta(ctx.scope))


def action_table(db, ctx):
    from app.models import PvSafetyAction

    scope = ctx.scope
    actions = db.scalars(select(PvSafetyAction).where(
        PvSafetyAction.pv_product_id == ctx.report.pv_product_id
    ).order_by(PvSafetyAction.action_date)).all()
    undated = [a for a in actions if a.action_date is None]
    shown = [a for a in actions if a.action_date
             and scope.period_start <= a.action_date <= scope.period_end]
    if not shown and not undated:
        raise TableUnavailable("no action for safety reasons falls in the interval")
    rows = [[grids.text(a.action_date.isoformat()), grids.text(a.region),
             grids.text(a.action_type.replace("_", " ")), grids.text(a.description),
             grids.text(a.reason)] for a in shown]
    missing = [f"action '{a.description or a.action_type}' has no date and cannot "
               "be placed in or out of the interval" for a in undated]
    if not rows:
        raise TableUnavailable(
            "no dated action for safety reasons falls in the interval")
    return _finish("action_table",
                   "Actions taken in the reporting interval for safety reasons",
                   ["Date", "Region", "Action", "Description", "Reason"], rows,
                   totals={"actions": len(rows)}, scope=_scope_meta(scope),
                   missing=missing)


def study_inventory(db, ctx):
    from app.models import PvStudy

    scope = ctx.scope
    studies = db.scalars(select(PvStudy).where(
        PvStudy.pv_product_id == ctx.report.pv_product_id
    ).order_by(PvStudy.study_id)).all()
    shown = [s for s in studies
             if (s.start_date is None or s.start_date <= scope.period_end)
             and (s.completion_date is None
                  or s.completion_date >= scope.period_start)]
    if not shown:
        raise TableUnavailable("no trial was ongoing or completed in the period")
    rows = [[grids.text(s.study_id), grids.text(s.title), grids.text(s.phase),
             grids.text(s.status), grids.text(s.population),
             grids.text(s.planned_enrolment), grids.text(s.actual_enrolment)]
            for s in shown]
    missing = [f"study {s.study_id} has no start date; it is included on the "
               "assumption it began before the period ended"
               for s in shown if s.start_date is None]
    return _finish("study_inventory",
                   "Inventory of clinical trials ongoing and completed during the "
                   "reporting period",
                   ["Study", "Title", "Phase", "Status", "Population",
                    "Planned enrolment", "Actual enrolment"], rows,
                   totals={"studies": len(rows)}, scope=_scope_meta(scope),
                   missing=missing)


def approval_status_table(db, ctx):
    from app.models import PvApprovalStatus

    rows_db = db.scalars(select(PvApprovalStatus).where(
        PvApprovalStatus.pv_product_id == ctx.report.pv_product_id
    ).order_by(PvApprovalStatus.country)).all()
    if not rows_db:
        raise TableUnavailable("no marketing approval status is recorded")
    rows = [[grids.text(a.country), grids.text(a.status.replace("_", " ")),
             grids.text(a.approval_date.isoformat() if a.approval_date else None),
             grids.text(a.indication), grids.text(a.formulation)] for a in rows_db]
    return _finish("approval_status_table", "Worldwide marketing approval status",
                   ["Country", "Status", "Date", "Indication", "Formulation"], rows,
                   totals={"countries": len(rows)}, scope=_scope_meta(ctx.scope))


def literature_table(db, ctx):
    from app.models import PvLiteratureRef

    scope = ctx.scope
    refs = db.scalars(select(PvLiteratureRef).where(
        PvLiteratureRef.pv_product_id == ctx.report.pv_product_id
    ).order_by(PvLiteratureRef.search_date)).all()
    shown = [r for r in refs if r.search_date is None
             or scope.period_start <= r.search_date <= scope.data_lock_point]
    if not shown:
        raise TableUnavailable("no literature reference falls in the interval")
    rows = [[grids.text(r.citation), grids.text(r.database),
             grids.text(r.search_date.isoformat() if r.search_date else None),
             grids.text(r.relevance), grids.text(len(r.linked_case_ids or []))]
            for r in shown]
    return _finish("literature_table", "Literature references",
                   ["Citation", "Database", "Searched", "Relevance", "Linked cases"],
                   rows, totals={"references": len(rows)},
                   scope=_scope_meta(scope))


BUILDERS = {
    "summary_tab_soc_pt": summary_tab_soc_pt,
    "summary_tab_trials": summary_tab_trials,
    "line_listing_sar": line_listing_sar,
    "exposure_table": exposure_table,
    "signal_overview": signal_overview,
    "safety_concern_table": safety_concern_table,
    "action_table": action_table,
    "study_inventory": study_inventory,
    "approval_status_table": approval_status_table,
    "literature_table": literature_table,
}


@dataclass
class Context:
    report: object
    product: object
    scope: scope_mod.Scope


def render(db, *, report, product, table_key: str) -> Tabulation:
    """One table for one report, or a `TableError` saying why not."""
    builder = BUILDERS.get(table_key)
    if builder is None:
        raise UnknownTable(
            f"no table is called {table_key!r}; one of {', '.join(sorted(BUILDERS))}")
    ctx = Context(report=report, product=product,
                  scope=scope_mod.scope_for(product, report))
    return builder(db, ctx)


def cases_for_cell(tabulation: Tabulation, cell_id: str) -> dict:
    """The events and cases behind one cell, for the drill-down."""
    return tabulation.cells.get(cell_id, {"events": [], "cases": []})
